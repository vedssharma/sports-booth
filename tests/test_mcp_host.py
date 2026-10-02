import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from booth import mcp_host, odds, orchestrator
from booth.cache import ttl_cache


def test_ttl_cache_expires_and_skips_errors():
    now = [0.0]
    calls = []

    @ttl_cache(10, clock=lambda: now[0])
    def fetch(x):
        calls.append(x)
        if x == "bad":
            raise RuntimeError
        return x.upper()

    assert fetch("a") == "A" and fetch("a") == "A" and calls == ["a"]
    now[0] = 11
    assert fetch("a") == "A" and calls == ["a", "a"]
    for _ in range(2):
        try:
            fetch("bad")
        except RuntimeError:
            pass
    assert calls.count("bad") == 2  # failures are retried, never cached


def test_odds_snapshot_is_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setattr(odds, "DB_PATH", tmp_path / "o.db")
    monkeypatch.setitem(odds._last, "at", float("-inf"))
    fetches = []
    monkeypatch.setattr(odds, "fetch_events", lambda key: fetches.append(key) or [])
    odds.snapshot_now("k"); odds.snapshot_now("k"); odds.snapshot_now("k")
    assert len(fetches) == 1  # three betting tool calls, one Odds API request


def test_mcp_config_prefers_running_http_servers(monkeypatch):
    monkeypatch.setattr(orchestrator, "_http_urls", {})
    assert orchestrator._mcp_config("rag")["type"] == "stdio"
    orchestrator.use_http_servers({"rag": "http://127.0.0.1:1/mcp"})
    assert orchestrator._mcp_config("rag") == {"type": "http", "url": "http://127.0.0.1:1/mcp"}
    assert orchestrator._mcp_config("nba")["type"] == "stdio"   # others unaffected


def test_host_starts_servers_and_agents_can_call_tools(monkeypatch):
    monkeypatch.setenv("BOOTH_MOCK_DATA", "1")
    monkeypatch.delenv("ODDS_API_KEY", raising=False)

    async def call(url, tool, args):
        async with streamable_http_client(url) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return (await session.call_tool(tool, args)).content[0].text

    async def go():
        host = mcp_host.McpHost()
        try:
            assert await host.start()
            assert set(host.urls) == {"nba", "rag", "betting"}
            # several concurrent calls against one long-lived server
            outs = await asyncio.gather(*[call(host.urls["betting"], "get_live_odds", {})
                                          for _ in range(3)])
            assert all("MOCK DATA" in o for o in outs)
            assert "Lakers" in await call(host.urls["nba"], "get_live_scoreboard", {}) \
                or "LAL" in await call(host.urls["nba"], "get_live_scoreboard", {})
        finally:
            await host.stop()
        assert host.urls == {}

    asyncio.run(go())


def test_host_reports_failure_and_cleans_up(monkeypatch):
    monkeypatch.setattr(mcp_host, "SERVER_SCRIPTS", {"nba": "does_not_exist.py"})

    async def go():
        host = mcp_host.McpHost()
        assert await host.start() is False
        assert host.urls == {} and host._procs == []

    asyncio.run(go())


def test_mcp_server_logs_follow_the_booth_format_and_stay_quiet(monkeypatch):
    """In JSON mode every line a server process writes must be JSON (one broken line breaks log
    shipping), and the MCP library's per-request INFO chatter must not appear at all."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).parent.parent
    env = {**os.environ, "BOOTH_LOG_FORMAT": "json", "BOOTH_MOCK_DATA": "1"}
    env.pop("BOOTH_LOG_LEVEL", None)
    proc = subprocess.Popen([sys.executable, "mcp_servers/betting_server.py", "--http", "--port", "8151"],
                            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    async def exercise():
        for _ in range(60):
            try:
                async with streamable_http_client("http://127.0.0.1:8151/mcp") as (r, w, _):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        await session.list_tools()
                        await session.call_tool("get_live_odds", {})
                        return
            except Exception:
                await asyncio.sleep(0.25)
        raise AssertionError("server never came up")

    try:
        asyncio.run(exercise())
    finally:
        proc.terminate()
        _, err = proc.communicate(timeout=15)
    lines = [line for line in err.splitlines() if line.strip()]
    for line in lines:
        json.loads(line)            # raises on any non-JSON line
    assert not any("Processing request" in line for line in lines)
