"""Turns a detected game event into broadcast commentary."""
from booth.budget import BudgetGuard
from booth.history import history
from booth.orchestrator import CONTEXT_MOMENTS, run_booth_commentary
from booth.policy import CommentaryPolicy
from booth.server import manager


budget = BudgetGuard()
policy = CommentaryPolicy(budget)

_ROLE_ICONS = {"analyst": "📊 ANALYST:   ", "historian": "📚 HISTORIAN: ", "degenerate": "🎲 DEGENERATE:"}


async def process_event(event: dict, cli_only: bool = False) -> None:
    label = event.get("event", event.get("type", "event"))
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"  {event.get('game', '')}  |  Q{event.get('quarter', '')} {event.get('time_remaining', '')}  |  {event.get('score', '')}")

    decision = policy.decide(event)
    change = budget.transition()
    if change and not cli_only:
        notice = {"saver": "Hourly budget nearly used — switching to cheaper, sparser commentary.",
                  "exhausted": "Hourly budget reached — commentary paused except final scores."}.get(change[1])
        if notice:
            await manager.broadcast({"type": "status", "message": notice})
    if change:
        print(f"  💰 Budget state: {change[0]} → {change[1]}  {budget.snapshot()}")
    if decision is None:
        print("  ⏭  Skipped (budget)")
        return

    if not cli_only:
        # `agents` tells the dashboard which columns to show "thinking" in
        await manager.broadcast({"type": "event", "data": {**event, "agents": list(decision.agents)}})

    print(f"  Fetching booth commentary: {', '.join(decision.agents)} on {decision.model} ({decision.reason})…")
    earlier = history.recent_for_game(event.get("game_id", ""), CONTEXT_MOMENTS)
    commentary = await run_booth_commentary(event, model=decision.model, agents=decision.agents,
                                            earlier=earlier)
    budget.record(commentary["cost_usd"])

    for role in decision.agents:
        print(f"\n  {_ROLE_ICONS[role]} {commentary[role][:200]}")
    print(f"\n  💰 ${commentary['cost_usd']:.4f} this event | {budget.snapshot()}")

    history.add(commentary)  # before broadcasting, so a snapshot never misses a sent item
    if not cli_only:
        await manager.broadcast({"type": "commentary", "data": commentary})
