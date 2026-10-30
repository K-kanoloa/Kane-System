"""Versioned local host IPC. No SQL or system state machine lives in Python."""
from __future__ import annotations

import asyncio
import json
import os
import struct
from pathlib import Path
from typing import Any


class CoreUnavailable(RuntimeError):
    pass


class CoreRejected(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        self.status = status
        super().__init__(message)


class SystemClient:
    def __init__(self) -> None:
        root = Path(__file__).resolve().parents[4]
        self.binary = Path(os.getenv("KANE_SYSTEM_BINARY", str(root / "target/debug/kanesys")))
        self.database = Path(os.getenv("KANE_SYSTEM_DB", str(root / ".kane/system.db")))
        self.socket = os.getenv("KANE_SYSTEM_SOCKET")
        self._slots = asyncio.Semaphore(8)

    async def request(self, operation: str, **payload: Any) -> Any:
        body = json.dumps({"op": operation, **payload}, ensure_ascii=False).encode()
        if len(body) > 2_000_000:
            raise CoreRejected("request too large", 413)
        async with self._slots:
            if self.socket:
                # System service mode never falls back to opening the database or
                # starting another core when the configured daemon is unavailable.
                return await self._socket_request(operation, payload)
            if not self.binary.is_file():
                raise CoreUnavailable("Rust core is not built")
            process = await asyncio.create_subprocess_exec(
                str(self.binary), "--db", str(self.database), "host-request",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(body), timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            if process.returncode:
                # Do not expose command output, content or paths through the API.
                text = stderr.decode(errors="replace")
                status, code = 400, "system_request_rejected"
                if "version/lease conflict" in text:
                    status, code = 409, "system_version_conflict"
                elif "not found:" in text:
                    status, code = 404, "system_object_not_found"
                elif "permission denied:" in text:
                    status, code = 403, "system_permission_denied"
                elif "reconciliation required" in text:
                    status, code = 409, "system_reconciliation_required"
                elif "database:" in text:
                    status, code = 503, "system_database_unavailable"
                raise CoreRejected(code, status)
            try:
                return json.loads(stdout)
            except (ValueError, UnicodeError) as exc:
                raise CoreUnavailable("invalid core response") from exc

    async def _socket_request(self, operation: str, payload: dict[str, Any]) -> Any:
        frame = json.dumps({"protocol": 1, "request": {"op": operation, **payload}}, ensure_ascii=False).encode()
        if len(frame) > 2_000_000:
            raise CoreRejected("request too large", 413)
        writer = None
        try:
            async with asyncio.timeout(15):
                reader, writer = await asyncio.open_unix_connection(self.socket)
                writer.write(struct.pack("!I", len(frame)) + frame)
                await writer.drain()
                size = struct.unpack("!I", await reader.readexactly(4))[0]
                if not 0 < size <= 16_000_000:
                    raise CoreUnavailable("invalid core response")
                response = json.loads(await reader.readexactly(size))
                if not isinstance(response, dict) or response.get("protocol") != 1 or not isinstance(response.get("ok"), bool):
                    raise CoreUnavailable("invalid core response")
                if not response["ok"]:
                    # Only fixed codes produced by the trusted Rust boundary.
                    error = response.get("error", {})
                    if not isinstance(error, dict):
                        raise CoreUnavailable("invalid core response")
                    codes = {"system_version_conflict": 409, "system_object_not_found": 404,
                             "system_permission_denied": 403, "system_reconciliation_required": 409,
                             "system_database_unavailable": 503, "system_request_rejected": 400}
                    code = error.get("code", "system_request_rejected")
                    if not isinstance(code, str) or code not in codes:
                        code = "system_request_rejected"
                    raise CoreRejected(code, codes[code])
                return response["result"]
        except CoreRejected:
            raise
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError, KeyError, TypeError) as exc:
            # A lost response does not imply a mutation was not committed. No retry.
            raise CoreUnavailable("core service unavailable; inspect state before retrying") from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=1)
                except (OSError, TimeoutError):
                    writer.transport.abort()

