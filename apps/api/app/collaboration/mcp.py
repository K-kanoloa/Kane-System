"""Bounded trusted-Host MCP stdio tool adapter over the existing durable outbox.

The Host registers commands; Agents cannot choose executables or destinations.
This does not claim arbitrary MCP server sandboxing or A2A support.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from ..system.client import CoreRejected


class McpStdio:
    def __init__(self, command, timeout=5):
        if not command or not Path(command[0]).is_absolute() or not 0 < timeout <= 30:
            raise ValueError('Owner-registered absolute command and bounded timeout required')
        self.command, self.timeout = tuple(command), timeout
        self.server_sha256 = hashlib.sha256(json.dumps(list(command)).encode()).hexdigest()
        self.process = None
        self.counter = 0
        self.lock = asyncio.Lock()
        self.tools = {}

    async def rpc(self, method, params):
        async with self.lock, asyncio.timeout(self.timeout):
            self.counter += 1
            request = {'jsonrpc':'2.0','id':self.counter,'method':method,'params':params}
            raw = json.dumps(request).encode() + b'\n'
            if len(raw) > 65536 or self.process is None or self.process.returncode is not None:
                raise CoreRejected('MCP process or input unavailable')
            self.process.stdin.write(raw)
            await self.process.stdin.drain()
            for _ in range(32):
                line = await self.process.stdout.readline()
                if not line:
                    raise CoreRejected('MCP disconnected; no automatic replay')
                reply = json.loads(line)
                if reply.get('id') != self.counter or reply.get('jsonrpc') != '2.0':
                    raise CoreRejected('MCP mismatched/untrusted response')
                if 'error' in reply:
                    raise CoreRejected('MCP error; execution status requires reconciliation')
                return reply['result']
            raise CoreRejected('MCP notification limit exceeded')

    async def start(self):
        if self.process is not None:
            raise CoreRejected('MCP process already owned')
        self.process = await asyncio.create_subprocess_exec(*self.command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1048576, env={'PATH':os.defpath,'LANG':'C.UTF-8'})
        result = await self.rpc('initialize', {'protocolVersion':'2025-06-18','capabilities':{},
            'clientInfo':{'name':'kane-system-host','version':'0.3'}})
        if result.get('protocolVersion') != '2025-06-18' or 'tools' not in result.get('capabilities', {}):
            raise CoreRejected('MCP tool capability/version not verified')
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await self.process.stdin.drain()
        result = await self.rpc('tools/list', {})
        self.tools = {tool['name']:tool for tool in result['tools']}
        return result

    async def call(self, core, turn, native, key, tool, arguments):
        if tool not in self.tools:
            raise CoreRejected('Undiscovered MCP tool')
        action = await core.request('external_prepare',turn_id=turn,native_session_ref=native,key=key,
            protocol='mcp',server_sha256=self.server_sha256,tool=tool,arguments=arguments)
        # A duplicate Prepared/Confirmed/Unknown action never causes another RPC.
        if action['state'] != 'Pending':
            raise CoreRejected('Inspect durable MCP action; do not replay')
        await core.request('external_claim',id=action['id'])
        try:
            result = await self.rpc('tools/call', {'name':tool,'arguments':arguments})
            if not isinstance(result.get('content'), list):
                raise CoreRejected('Invalid MCP result')
            receipt = {'server_sha256':self.server_sha256,'result_sha256':hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest(),
                       'isError':result.get('isError',False),'validation':'transport result only, not Task success'}
            await core.request('external_result',id=action['id'],evidence=json.dumps(receipt,sort_keys=True))
            return result
        except BaseException:
            # If Core is down the durable Dispatching remains; existing recovery
            # promotes it to Unknown. Failure to record is never permission to retry.
            try:
                await asyncio.shield(core.request('external_unknown',id=action['id']))
            except Exception:
                pass
            raise

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
