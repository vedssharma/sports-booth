"""Rolling-window spend tracking so an always-on booth can't quietly run up a bill."""
import os
import time
from collections import deque

WINDOW_S = 3600
SAVER_FRACTION = 0.75   # at 75% of the hourly cap, switch to cheap-only mode
DEFAULT_USD_PER_HOUR = float(os.getenv("BOOTH_BUDGET_USD_PER_HOUR", "5"))


class BudgetGuard:
    """
    States:
      ok         — normal operation
      saver      — >=75% of the hourly cap spent: cheap model everywhere, no routine updates
      exhausted  — cap reached: only final scores get commentary until spend rolls out of the window
    A cap of 0 disables the guard (always "ok").
    """

    def __init__(self, usd_per_hour: float = DEFAULT_USD_PER_HOUR, clock=time.monotonic) -> None:
        self.cap = usd_per_hour
        self._clock = clock
        self._spend: deque[tuple[float, float]] = deque()
        self._last_state = "ok"

    def record(self, cost_usd: float) -> None:
        if cost_usd > 0:
            self._spend.append((self._clock(), cost_usd))

    def spent(self) -> float:
        cutoff = self._clock() - WINDOW_S
        while self._spend and self._spend[0][0] < cutoff:
            self._spend.popleft()
        return sum(c for _, c in self._spend)

    def state(self) -> str:
        if not self.cap:
            return "ok"
        spent = self.spent()
        if spent >= self.cap:
            return "exhausted"
        return "saver" if spent >= self.cap * SAVER_FRACTION else "ok"

    def transition(self) -> tuple[str, str] | None:
        """Returns (old, new) once when the state changes, else None (for one-time notices)."""
        new = self.state()
        old, self._last_state = self._last_state, new
        return (old, new) if old != new else None

    def snapshot(self) -> dict:
        return {"state": self.state(), "spent_last_hour_usd": round(self.spent(), 4),
                "cap_usd_per_hour": self.cap or None}
