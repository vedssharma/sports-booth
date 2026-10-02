"""
Runs the MCP servers once, as long-lived local HTTP processes, instead of spawning a fresh
stdio subprocess for every agent run. The big win is the Historian's server, which otherwise
reloads ChromaDB and the sentence-transformer model on every single event.
"""
import asyncio
import atexit
import socket
import sys
from pathlib import Path

from booth.orchestrator import SERVER_SCRIPTS

ROOT = Path(__file__).parent.parent
STARTUP_TIMEOUT_S = 30


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_port(port: int, proc: asyncio.subprocess.Process, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if proc.returncode is not None:
            return False
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return True
        except OSError:
            await asyncio.sleep(0.2)
    return False


class McpHost:
    def __init__(self) -> None:
        self.urls: dict[str, str] = {}
        self._procs: list[asyncio.subprocess.Process] = []

    async def start(self) -> bool:
        """Start every server. Returns False (after cleaning up) if any fails to come up,
        so the caller can fall back to per-query stdio servers."""
        atexit.register(self._kill_all)
        for name, script in SERVER_SCRIPTS.items():
            port = _free_port()
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(ROOT / "mcp_servers" / script), "--http", "--port", str(port))
            self._procs.append(proc)
            if not await _wait_for_port(port, proc, STARTUP_TIMEOUT_S):
                print(f"  ⚠️  MCP server '{name}' failed to start")
                await self.stop()
                return False
            self.urls[name] = f"http://127.0.0.1:{port}/mcp"
        return True

    def _kill_all(self) -> None:
        for proc in self._procs:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass

    async def stop(self) -> None:
        self._kill_all()
        for proc in self._procs:
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
        self._procs.clear()
        self.urls.clear()
