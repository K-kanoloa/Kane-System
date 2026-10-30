"""Attach-only ACP transport. Rust owns the runtime; never launch or restart it."""
from __future__ import annotations

import asyncio
import json
import os


class SupervisedTransport:
    def __init__(self, reader, writer, pid):
        self.stdout = reader
        self.stdin = writer
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.pid = pid
        self._closed = False

    @property
    def returncode(self):
        # Transport closure is not a trustworthy runtime exit code.
        return 255 if self._closed or self.stdout.at_eof() else None

    def terminate(self):
        self._closed = True
        self.stdin.close()

    kill = terminate

    async def wait(self):
        await self.stdin.wait_closed()
        self._closed = True
        return 255

    @classmethod
    async def connect(cls, socket: str, agent_id: str, cwd: str):
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(socket), 5)
        try:
            writer.write(b'{"op":"attach"}\n')
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if not response.get("ok") or response.get("agent_id") != agent_id or response.get("cwd") != os.path.realpath(cwd):
                raise RuntimeError("Supervisor attachment identity/workspace mismatch")
            return cls(reader, writer, response["pid"])
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise
