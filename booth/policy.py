"""
Decides, per event, whether to generate commentary, with which model, and which personas.

Not every moment deserves three Sonnet-class agent runs. Big moments get the full booth on the
main model; routine events get the cheap model, and a routine "live update" only one persona
(rotating, so every voice still gets airtime). A BudgetGuard degrades this further as spend
approaches the hourly cap.
"""
from dataclasses import dataclass

from booth.budget import BudgetGuard
from booth.orchestrator import ALL_AGENTS, FAST_MODEL, MODEL

BIG_MOMENTS = {"scoring_run", "close_game", "game_final", "lead_change", "player_milestone"}
# Worth a quick, cheap take (analyst on the lineup impact, degenerate on the line) — never the full booth
FOUL_TROUBLE_AGENTS = ("analyst", "degenerate")


@dataclass(frozen=True)
class Decision:
    model: str
    agents: tuple[str, ...]
    reason: str


class CommentaryPolicy:
    def __init__(self, budget: BudgetGuard, main_model: str = MODEL, fast_model: str = FAST_MODEL) -> None:
        self.budget = budget
        self.main_model = main_model
        self.fast_model = fast_model
        self._rotation: dict[str, int] = {}

    def decide(self, event: dict) -> Decision | None:
        """None means: skip this event (budget)."""
        etype = event.get("type")
        state = self.budget.state()
        routine = etype == "game_update" and not event.get("join")
        low_value = routine or etype == "foul_trouble"

        if state == "exhausted":
            # Finals are rare and cheap relative to what they're worth; everything else waits.
            if etype == "game_final":
                return Decision(self.fast_model, ALL_AGENTS, "budget exhausted: final only")
            return None
        if state == "saver":
            if low_value:
                return None
            return Decision(self.fast_model, ALL_AGENTS, "budget saver: cheap model")

        if etype == "foul_trouble":
            return Decision(self.fast_model, FOUL_TROUBLE_AGENTS, "foul trouble: analyst + degenerate")
        if routine:
            gid = event.get("game_id", "")
            idx = self._rotation[gid] = (self._rotation.get(gid, -1) + 1) % len(ALL_AGENTS)
            return Decision(self.fast_model, (ALL_AGENTS[idx],), "routine update: one persona")
        if etype in BIG_MOMENTS or etype not in ("quarter_start", "game_update"):
            # Unknown types (e.g. demo events) count as big
            return Decision(self.main_model, ALL_AGENTS, "big moment")
        return Decision(self.fast_model, ALL_AGENTS, "routine moment: cheap model")
