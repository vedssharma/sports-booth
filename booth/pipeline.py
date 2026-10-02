"""Turns a detected game event into broadcast commentary."""
from booth.orchestrator import run_booth_commentary
from booth.server import manager


async def process_event(event: dict, cli_only: bool) -> None:
    label = event.get("event", event.get("type", "event"))
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"  {event.get('game', '')}  |  Q{event.get('quarter', '')} {event.get('time_remaining', '')}  |  {event.get('score', '')}")

    if not cli_only:
        await manager.broadcast({"type": "event", "data": event})

    print("  Fetching booth commentary (3 agents in parallel)…")
    commentary = await run_booth_commentary(event)

    print(f"\n  📊 ANALYST:    {commentary['analyst'][:200]}")
    print(f"\n  📚 HISTORIAN:  {commentary['historian'][:200]}")
    print(f"\n  🎲 DEGENERATE: {commentary['degenerate'][:200]}")

    if not cli_only:
        await manager.broadcast({"type": "commentary", "data": commentary})
