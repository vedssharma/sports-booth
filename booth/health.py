"""Runtime health: what the process knows about whether it is doing its job."""
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from booth import metrics

ROOT = Path(__file__).parent.parent
FAILED_POLLS_BEFORE_DEGRADED = 3
MIN_STALE_POLL_S = 120


def _version() -> str:
    try:
        return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    except Exception:
        return "unknown"


@dataclass
class RuntimeState:
    mode: str = "starting"              # live | demo | cli-live | cli-demo
    mcp_transport: str = "unknown"      # http | stdio
    poll_interval_s: float = 45.0
    consecutive_poll_failures: int = 0
    last_poll_ok_at: float | None = None
    started_at: float = field(default_factory=time.time)
    version: str = field(default_factory=_version)

    def poll_ok(self) -> None:
        self.consecutive_poll_failures = 0
        self.last_poll_ok_at = time.time()
        metrics.registry.inc("scoreboard_polls_total", outcome="ok")
        metrics.registry.set("last_successful_poll_timestamp_seconds", self.last_poll_ok_at)

    def poll_failed(self) -> None:
        self.consecutive_poll_failures += 1
        metrics.registry.inc("scoreboard_polls_total", outcome="error")

    def report(self, *, clients: int, scheduler: dict | None, budget: dict) -> dict:
        """The /health payload. `status` is "degraded" (with reasons) when something needs attention."""
        now = time.time()
        reasons = []
        live = self.mode.endswith("live")
        if live and self.consecutive_poll_failures >= FAILED_POLLS_BEFORE_DEGRADED:
            reasons.append(f"scoreboard polling failed {self.consecutive_poll_failures} times in a row")
        if live and self.last_poll_ok_at is not None \
                and now - self.last_poll_ok_at > max(3 * self.poll_interval_s, MIN_STALE_POLL_S):
            reasons.append(f"no successful scoreboard poll for {int(now - self.last_poll_ok_at)}s")
        if budget.get("state") == "exhausted":
            reasons.append("hourly budget exhausted: commentary limited to final scores")
        return {
            "status": "degraded" if reasons else "ok",
            "reasons": reasons,
            "version": self.version,
            "mode": self.mode,
            "uptime_s": int(now - self.started_at),
            "mcp_transport": self.mcp_transport,
            "clients": clients,
            "polling": None if not live else {
                "interval_s": self.poll_interval_s,
                "consecutive_failures": self.consecutive_poll_failures,
                "last_success_age_s": None if self.last_poll_ok_at is None else int(now - self.last_poll_ok_at),
            },
            "scheduler": scheduler,
            "budget": budget,
            "agents": self._agent_summary(),
        }

    @staticmethod
    def _agent_summary() -> dict:
        snap = metrics.registry.snapshot()
        runs, errors = {}, {}
        for row in snap.get("agent_runs_total", []):
            who = row["labels"].get("persona", "?")
            runs[who] = runs.get(who, 0) + row["value"]
            if row["labels"].get("outcome") == "error":
                errors[who] = errors.get(who, 0) + row["value"]
        latency = {r["labels"].get("persona", "?"): {"p50_s": r["p50"], "p95_s": r["p95"], "count": r["count"]}
                   for r in snap.get("agent_run_seconds", [])}
        return {"runs": runs, "errors": errors, "latency": latency,
                "tool_calls": metrics.registry.total("agent_tool_calls_total"),
                "tool_unavailable": metrics.registry.total("agent_tool_unavailable_total")}


runtime = RuntimeState()
