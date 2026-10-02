"""Turns a detected game event into broadcast commentary."""
import uuid

from booth import log, metrics
from booth.budget import BudgetGuard
from booth.history import history
from booth.orchestrator import CONTEXT_MOMENTS, run_booth_commentary
from booth.policy import CommentaryPolicy
from booth.streaming import StreamRelay
from booth.server import manager


budget = BudgetGuard()
policy = CommentaryPolicy(budget)
logger = log.get("pipeline")

_ROLE_ICONS = {"analyst": "📊 ANALYST:   ", "historian": "📚 HISTORIAN: ", "degenerate": "🎲 DEGENERATE:"}


async def process_event(event: dict, cli_only: bool = False) -> None:
    event_id = uuid.uuid4().hex[:8]
    with log.bind(event_id=event_id, game_id=event.get("game_id"), event_type=event.get("type")):
        await _process(event, cli_only)


async def _process(event: dict, cli_only: bool) -> None:
    label = event.get("event", event.get("type", "event"))
    logger.info(label, extra={"game": event.get("game"), "quarter": event.get("quarter"),
                              "clock": event.get("time_remaining")})

    decision = policy.decide(event)
    change = budget.transition()
    if change:
        logger.warning("budget state changed", extra={"from_state": change[0], "to_state": change[1],
                                                      **budget.snapshot()})
        notice = {"saver": "Hourly budget nearly used — switching to cheaper, sparser commentary.",
                  "exhausted": "Hourly budget reached — commentary paused except final scores."}.get(change[1])
        if notice and not cli_only:
            await manager.broadcast({"type": "status", "message": notice})
    if decision is None:
        metrics.registry.inc("events_skipped_budget_total", type=event.get("type", "unknown"))
        logger.info("event skipped by budget guard")
        return

    if not cli_only:
        # `agents` tells the dashboard which columns to show "thinking" in
        await manager.broadcast({"type": "event", "data": {**event, "agents": list(decision.agents)}})

    logger.info("generating commentary", extra={"agents": ",".join(decision.agents), "model": decision.model,
                                                 "reason": decision.reason})
    earlier = history.recent_for_game(event.get("game_id", ""), CONTEXT_MOMENTS)
    relay = None if cli_only else StreamRelay(manager, event.get("game_id", ""))
    try:
        commentary = await run_booth_commentary(event, model=decision.model, agents=decision.agents,
                                                earlier=earlier, on_stream=relay)
    finally:
        if relay:
            await relay.close()
    budget.record(commentary["cost_usd"])
    logger.info("commentary ready", extra={"cost_usd": commentary["cost_usd"], "model": decision.model,
                                           "spent_last_hour_usd": budget.snapshot()["spent_last_hour_usd"]})

    if cli_only:   # the console *is* the product in --cli mode
        print(f"\n{'─'*60}\n  {label}")
        for role in decision.agents:
            print(f"\n  {_ROLE_ICONS[role]} {commentary[role][:200]}")

    history.add(commentary)  # before broadcasting, so a snapshot never misses a sent item
    if not cli_only:
        await manager.broadcast({"type": "commentary", "data": commentary})
