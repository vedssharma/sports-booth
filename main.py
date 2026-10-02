"""
Sports Booth — entry point.

CLI usage:
  uv run python main.py              # live NBA games, web dashboard at http://127.0.0.1:8000
  uv run python main.py --demo       # use hardcoded demo events instead of live data
  uv run python main.py --cli        # no web server, terminal output only
  uv run python main.py --interval 60  # seconds between scoreboard polls (default: 45)
  uv run python main.py --check-config # validate the environment, print the effective config, exit
"""
import argparse
import asyncio
import os
import sys
from functools import partial

import uvicorn
from dotenv import load_dotenv

load_dotenv()

from booth import config  # noqa: E402  (booth modules read settings at import: load .env first)


def _parse_args(settings: config.Settings) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sports Booth — AI commentary system")
    parser.add_argument("--demo", action="store_true", help="Use hardcoded demo events instead of live NBA data")
    parser.add_argument("--cli", action="store_true", help="CLI-only mode (no web server)")
    parser.add_argument("--interval", type=int, default=45, help="Seconds between polls/events (default: 45)")
    parser.add_argument("--host", default=settings.host,
                        help="Interface to bind (default: 127.0.0.1, or BOOTH_HOST). "
                             "Non-loopback addresses require BOOTH_AUTH_TOKEN")
    parser.add_argument("--port", type=int, default=settings.port,
                        help="Web server port (default: 8000, or BOOTH_PORT)")
    parser.add_argument("--stdio-mcp", action="store_true",
                        help="Spawn MCP servers per agent run (slower) instead of keeping them running")
    parser.add_argument("--check-config", action="store_true",
                        help="Validate configuration, print the effective settings and exit")
    return parser.parse_args()


async def _run(interval: int, cli_only: bool, demo: bool, host: str, port: int, stdio_mcp: bool) -> None:
    from booth.mcp_host import McpHost
    from booth.orchestrator import use_http_servers

    mcp = None
    if not stdio_mcp:
        mcp = McpHost()
        print("  Starting MCP servers…")
        if await mcp.start():
            use_http_servers(mcp.urls)
        else:
            print("  ⚠️  Falling back to per-query stdio MCP servers")
            mcp = None

    try:
        await _serve(interval, cli_only, demo, host, port)
    finally:
        if mcp:
            await mcp.stop()


async def _serve(interval: int, cli_only: bool, demo: bool, host: str, port: int) -> None:
    from booth.demo import demo_loop
    from booth.live import live_loop
    from booth.pipeline import process_event
    from booth.scheduler import EventScheduler
    from booth.server import app

    if demo:
        # Demo plays its scripted events in order, waiting for each — no scheduler needed
        async def loop_fn() -> None:
            await demo_loop(interval, cli_only)
    else:
        scheduler = EventScheduler(partial(process_event, cli_only=cli_only))
        app.state.scheduler = scheduler

        async def loop_fn() -> None:
            await live_loop(interval, cli_only, scheduler)

    if cli_only:
        await loop_fn()
        return

    server_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(server_config)

    async def _start_loop() -> None:
        await asyncio.sleep(1)  # let server bind
        print(f"  Dashboard: http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}")
        await loop_fn()

    await asyncio.gather(server.serve(), _start_loop())


def main() -> None:
    try:
        settings = config.load_settings()
    except config.ConfigError as e:
        print(f"⚠️  {e}", file=sys.stderr)
        sys.exit(2)
    args = _parse_args(settings)

    if args.demo:
        # Mock tool data and in-memory history are only for demo mode; live mode reports
        # "unavailable" instead of inventing data. Must be set before booth modules import.
        os.environ["BOOTH_MOCK_DATA"] = "1"
        config.reset()
    try:
        settings = config.get()
    except config.ConfigError as e:
        print(f"⚠️  {e}", file=sys.stderr)
        sys.exit(2)
    settings = config.Settings(**{**settings.__dict__, "host": args.host, "port": args.port})

    errors, warnings = config.check_runtime(settings, demo=args.demo)
    if args.check_config:
        print("Effective configuration:")
        for key, value in settings.describe().items():
            print(f"  {key:22} {value}")
        for w in warnings:
            print(f"⚠️  {w}")
        for e in errors:
            print(f"✗  {e}")
        sys.exit(2 if errors else 0)
    if errors:
        print("⚠️  Cannot start:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(2)
    for w in warnings:
        print(f"⚠️  {w}", file=sys.stderr)

    from booth.history import history
    from booth.orchestrator import FAST_MODEL, MODEL
    from booth.pipeline import budget

    if args.demo:
        history.use_memory()

    print("🏀 Sports Booth starting…")
    print(f"   Models:   {MODEL} (big moments), {FAST_MODEL} (routine)")
    print(f"   Budget:   {'unlimited' if not budget.cap else f'${budget.cap:g}/hour'} (BOOTH_BUDGET_USD_PER_HOUR)")
    print(f"   Interval: {args.interval}s")
    if args.demo:
        print("   ⚠️  Demo mode — using hardcoded Lakers vs Celtics events")

    asyncio.run(_run(args.interval, args.cli, args.demo, args.host, args.port, args.stdio_mcp))


if __name__ == "__main__":
    main()
