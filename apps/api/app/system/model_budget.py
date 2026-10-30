"""Narrow host-side model budget and managed-tool relay. Authority and persistence stay in Rust.

This is a same-UID development endpoint, not an untrusted-Agent security sandbox.
It exposes no Owner API, SQL, caller-supplied budget, refund or Task transition.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import stat
import struct


class ModelBudgetRelay:
    def __init__(self, bridge, path):
        self.bridge = bridge
        self.path = Path(path)
        self.server = None
        self.inode = None
        self.connections = set()

    async def start(self):
        if self.server is not None:
            return
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise RuntimeError("Private budget endpoint directory required")
        # Never unlink or silently replace a live/stale endpoint owned by another run.
        if self.path.exists() or self.path.is_symlink():
            raise RuntimeError("Budget endpoint exists; inspect previous owner")
        self.server = await asyncio.start_unix_server(self.handle, str(self.path), limit=16384)
        self.path.chmod(0o600)
        self.inode = self.path.stat().st_ino

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            async with asyncio.timeout(20):
                peer = writer.get_extra_info('socket')
                _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    raise PermissionError("peer UID")
                request = json.loads(await reader.readline())
                if not isinstance(request, dict):
                    raise ValueError("request")
                native = request.get('native_session_ref')
                turn = self.bridge.native_turns.get(native)
                if not turn or turn not in self.bridge.admitted:
                    raise PermissionError("unbound session")
                op = request.get('op')
                call = request.get('call_id')
                if not isinstance(call, str) or not 0 < len(call) <= 128:
                    raise ValueError("call id")
                if op == 'reserve':
                    result = await self.bridge.core.request('reserve_model', turn_id=turn,
                        call_id=call, native_session_ref=native,
                        provider=request.get('provider'), model=request.get('model'))
                elif op in {'claim', 'cancel'}:
                    result = await self.bridge.core.request(
                        'claim_model' if op == 'claim' else 'cancel_model', turn_id=turn,
                        call_id=call, native_session_ref=native)
                elif op == 'gateway_status':
                    result = await self.bridge.core.request('gateway_status', turn_id=turn, call_id=call)
                elif op == 'settle':
                    result = await self.bridge.core.request('settle_model', turn_id=turn,
                        call_id=call, receipt=request.get('receipt'))
                elif op == 'tool_read_ack':
                    result = await self.bridge.core.request('control_read_ack', turn_id=turn,call_id=call,native_session_ref=native,hash=request.get('hash'))
                elif op in {'tool_prepare', 'tool_claim', 'tool_result', 'tool_file'}:
                    # Core derives Task/Agent/Workspace from the admitted native turn.
                    result = await self.bridge.core.request(op, turn_id=turn,
                        call_id=call, native_session_ref=native,
                        tool=request.get('tool'), arguments=request.get('arguments'),
                        receipt=request.get('receipt'))
                else:
                    raise PermissionError("unsupported execution operation")
                writer.write((json.dumps({'ok': True, 'result': result})+'\n').encode())
                await writer.drain()
        except Exception:
            # Fixed error: do not return Core internals, prompts or credentials.
            writer.write(b'{"ok":false,"error":"model_budget_rejected_or_unavailable"}\n')
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                pass
        finally:
            writer.close()
            self.connections.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        tasks = list(self.connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.inode is not None and self.path.exists() and self.path.stat().st_ino == self.inode:
            self.path.unlink()
        self.inode = None
