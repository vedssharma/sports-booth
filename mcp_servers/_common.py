"""Shared helpers for the MCP servers (each server runs as a standalone script)."""
import argparse
import asyncio
import functools
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # so servers can import `booth`


def mock_enabled() -> bool:
    """Mock/demo data is only served when explicitly requested (demo mode)."""
    return os.getenv("BOOTH_MOCK_DATA") == "1"


def unavailable(source: str, reason: str) -> str:
    """Tool result for when real data can't be fetched. Never fabricates numbers."""
    return json.dumps({
        "error": f"{source} data unavailable: {reason}",
        "instruction": (
            "Do not invent or estimate numbers. Tell the listener this data is "
            "unavailable right now and comment only on what you can verify. "
            "Omit any [CHART] block."
        ),
    }, indent=2)


def offload(mcp):
    """Register a sync function as an MCP tool that runs in a worker thread.

    FastMCP calls sync tools directly on the event loop, so in a long-lived server one slow
    upstream call (nba_api, embeddings) would stall every other request. The decorator returns
    the original sync function, so tests and in-process callers are unaffected.
    """
    def deco(fn):
        @functools.wraps(fn)
        async def runner(*args, **kwargs):
            return await asyncio.to_thread(fn, *args, **kwargs)
        mcp.tool()(runner)
        return fn
    return deco


def serve(mcp, warmup=None) -> None:
    """Entry point for a server script: stdio by default, or `--http --port N` for a
    long-lived streamable-HTTP server shared by every agent run."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if not args.http:
        mcp.run()
        return
    # Same log format/level as the main process, and quiet the MCP library's per-request INFO chatter
    # (it would otherwise interleave rich-formatted text into a JSON log stream).
    import logging

    from booth import config, log
    settings = config.get()
    log.setup_logging(settings.log_level, settings.log_format, root=True)
    if settings.log_level != "DEBUG":
        for noisy in ("mcp", "httpx", "httpcore", "uvicorn", "sse_starlette"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    if warmup:
        import threading
        threading.Thread(target=warmup, daemon=True).start()
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = args.port
    mcp.settings.log_level = "WARNING"
    mcp.run(transport="streamable-http")
