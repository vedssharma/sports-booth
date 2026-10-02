"""
Sports Booth — entry point.

CLI usage:
  uv run python main.py              # live NBA games, web dashboard at http://localhost:8000
  uv run python main.py --demo       # use hardcoded demo events instead of live data
  uv run python main.py --cli        # no web server, terminal output only
  uv run python main.py --interval 60  # seconds between scoreboard polls (default: 45)
"""
import argparse
import asyncio
import os
from functools import partial

import uvicorn
from dotenv import load_dotenv

load_dotenv()

from booth.demo import demo_loop
from booth.live import live_loop
from booth.pipeline import process_event
from booth.scheduler import EventScheduler
from booth.server import app


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sports Booth — AI commentary system")
    parser.add_argument("--demo", action="store_true", help="Use hardcoded demo events instead of live NBA data")
    parser.add_argument("--cli", action="store_true", help="CLI-only mode (no web server)")
    parser.add_argument("--interval", type=int, default=45, help="Seconds between polls/events (default: 45)")
    parser.add_argument("--port", type=int, default=8000, help="Web server port (default: 8000)")
    return parser.parse_args()


async def _run(interval: int, cli_only: bool, demo: bool, port: int) -> None:
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

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)

    async def _start_loop() -> None:
        await asyncio.sleep(1)  # let server bind
        print(f"  Dashboard: http://localhost:{port}")
        await loop_fn()

    await asyncio.gather(server.serve(), _start_loop())


def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("⚠️  ANTHROPIC_API_KEY not set. Copy .env.example → .env and add your key.")
        return

    args = _parse_args()
    if args.demo:
        # Mock tool data is only allowed in demo mode; live mode reports "unavailable" instead.
        os.environ["BOOTH_MOCK_DATA"] = "1"

    print("🏀 Sports Booth starting…")
    print(f"   Model:    {os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-6')}")
    print(f"   Interval: {args.interval}s")
    if args.demo:
        print("   ⚠️  Demo mode — using hardcoded Lakers vs Celtics events")

    asyncio.run(_run(args.interval, args.cli, args.demo, args.port))


if __name__ == "__main__":
    main()
