"""Live mode: poll the NBA scoreboard and generate commentary on detected events."""
import asyncio
import os

from booth import odds
from booth.events import EventDetector, is_final
from booth.scheduler import EventScheduler
from booth.server import manager
from booth.sources import fetch_started_games, games_payload


async def live_loop(interval: int, cli_only: bool, scheduler: EventScheduler) -> None:
    """Poll the NBA live scoreboard and generate commentary on detected events."""
    detector = EventDetector()
    seen_ids: set[str] = set()
    warned_no_games = False

    print(f"  Mode: LIVE  |  Polling every {interval}s")

    while True:
        try:
            started = await fetch_started_games()
        except Exception as e:
            print(f"  ⚠️  Scoreboard fetch error: {e}. Retrying in {interval}s…")
            await asyncio.sleep(interval)
            continue

        live = [g for g in started if not is_final(g)]

        if live:
            warned_no_games = False
            # Broadcast current game list so the dashboard can render the selector
            if not cli_only:
                await manager.broadcast({"type": "games", "data": games_payload(live)})

            # Record opening odds the first time we see a game so line movement is real
            new_ids = {g["gameId"] for g in live} - seen_ids
            if new_ids and os.getenv("ODDS_API_KEY"):
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, odds.snapshot_now, os.environ["ODDS_API_KEY"])
                except Exception as e:
                    print(f"  ⚠️  Odds snapshot failed: {e}")
            seen_ids |= new_ids

        # Includes games that just went final, so the detector can announce them once
        for event in detector.detect(started):
            scheduler.submit(event)

        if not live and not warned_no_games:
            msg = "No live NBA games right now. Booth will activate automatically when games start."
            print(f"\n  ⏸  {msg}")
            if not cli_only:
                await manager.broadcast({"type": "status", "message": msg})
            warned_no_games = True

        print(f"\n  ⏱  Next poll in {interval}s…")
        await asyncio.sleep(interval)
