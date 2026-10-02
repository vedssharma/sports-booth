"""
In-process metrics: counters, gauges and latency summaries, exposed as JSON on /health and in
Prometheus text format on /metrics.

Every metric is declared once in METRICS (type + help). Recording an undeclared name raises, so a
typo fails in tests instead of silently creating a metric nobody graphs. There are no external
dependencies and no background threads; recording is a dict update under a lock.
"""
import threading
import time
from collections import deque

PREFIX = "booth_"
RESERVOIR = 500          # samples kept per summary series for quantiles
QUANTILES = (0.5, 0.95)

METRICS: dict[str, tuple[str, str]] = {
    # name: (type, help)
    "events_submitted_total": ("counter", "Game events handed to the scheduler, by event type."),
    "events_dropped_total": ("counter", "Events dropped before commentary: busy, superseded, overflow, stale."),
    "events_processed_total": ("counter", "Events that went through the commentary pipeline, by outcome."),
    "events_skipped_budget_total": ("counter", "Events skipped by the hourly budget guard."),
    "event_queue_wait_seconds": ("summary", "Time an event waited in its game's queue."),
    "event_process_seconds": ("summary", "Time to generate and broadcast commentary for one event."),
    "agent_runs_total": ("counter", "Agent runs by persona, model and outcome (ok/error)."),
    "agent_run_seconds": ("summary", "Wall-clock time of one agent run, by persona."),
    "agent_tool_calls_total": ("counter", "Tool calls made by agents, by persona and tool."),
    "agent_tool_unavailable_total": ("counter", "Tool results reporting a data source unavailable, by persona and tool."),
    "agent_tokens_total": ("counter", "Tokens used by agents, by persona and kind (input/output/cache_read/cache_creation)."),
    "agent_cost_usd_total": ("counter", "Reported agent spend in USD, by persona and model."),
    "scoreboard_polls_total": ("counter", "Scoreboard polls by outcome (ok/error)."),
    "last_successful_poll_timestamp_seconds": ("gauge", "Unix time of the last successful scoreboard poll."),
    "websocket_clients": ("gauge", "Connected dashboard clients."),
    "budget_spent_usd": ("gauge", "Spend in the rolling budget window."),
    "budget_cap_usd": ("gauge", "Hourly budget cap (0 = unlimited)."),
    "process_start_time_seconds": ("gauge", "Unix time the process started."),
}


def _key(labels: dict) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_labels(key: tuple, extra: dict | None = None) -> str:
    items = list(key) + sorted((extra or {}).items())
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in items) + "}" if items else ""


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._values: dict[str, dict[tuple, float]] = {}
            self._samples: dict[str, dict[tuple, dict]] = {}     # summaries: count, sum, reservoir

    def _declared(self, name: str, kind: str) -> None:
        if name not in METRICS:
            raise KeyError(f"undeclared metric {name!r}")
        if METRICS[name][0] != kind:
            raise TypeError(f"{name} is a {METRICS[name][0]}, not a {kind}")

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        self._declared(name, "counter")
        with self._lock:
            series = self._values.setdefault(name, {})
            k = _key(labels)
            series[k] = series.get(k, 0.0) + value

    def set(self, name: str, value: float, **labels) -> None:
        self._declared(name, "gauge")
        with self._lock:
            self._values.setdefault(name, {})[_key(labels)] = float(value)

    def observe(self, name: str, value: float, **labels) -> None:
        self._declared(name, "summary")
        with self._lock:
            s = self._samples.setdefault(name, {}).setdefault(
                _key(labels), {"count": 0, "sum": 0.0, "recent": deque(maxlen=RESERVOIR)})
            s["count"] += 1
            s["sum"] += value
            s["recent"].append(value)

    def value(self, name: str, **labels) -> float:
        """Current value of one counter/gauge series (0 if never recorded). Mostly for tests."""
        with self._lock:
            return self._values.get(name, {}).get(_key(labels), 0.0)

    def total(self, name: str) -> float:
        with self._lock:
            return sum(self._values.get(name, {}).values())

    @staticmethod
    def _quantile(sorted_samples: list[float], q: float) -> float:
        if not sorted_samples:
            return 0.0
        return sorted_samples[min(len(sorted_samples) - 1, int(q * len(sorted_samples)))]

    def snapshot(self) -> dict:
        """JSON-friendly view: {name: [{labels, value}] | [{labels, count, sum, p50, p95}]}."""
        out: dict = {}
        with self._lock:
            for name, series in self._values.items():
                out[name] = [{"labels": dict(k), "value": round(v, 6)} for k, v in sorted(series.items())]
            for name, series in self._samples.items():
                rows = []
                for k, s in sorted(series.items()):
                    ordered = sorted(s["recent"])
                    rows.append({"labels": dict(k), "count": s["count"], "sum": round(s["sum"], 4),
                                 **{f"p{int(q * 100)}": round(self._quantile(ordered, q), 4) for q in QUANTILES}})
                out[name] = rows
        return out

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, (kind, help_text) in METRICS.items():
                series = self._values.get(name) or self._samples.get(name)
                if not series:
                    continue
                full = PREFIX + name
                lines += [f"# HELP {full} {help_text}", f"# TYPE {full} {kind}"]
                if kind == "summary":
                    for k, s in sorted(self._samples[name].items()):
                        ordered = sorted(s["recent"])
                        for q in QUANTILES:
                            lines.append(f"{full}{_fmt_labels(k, {'quantile': str(q)})} {self._quantile(ordered, q):g}")
                        lines.append(f"{full}_sum{_fmt_labels(k)} {s['sum']:g}")
                        lines.append(f"{full}_count{_fmt_labels(k)} {s['count']}")
                else:
                    for k, v in sorted(self._values[name].items()):
                        lines.append(f"{full}{_fmt_labels(k)} {v:g}")
        return "\n".join(lines) + "\n"


registry = Registry()
registry.set("process_start_time_seconds", time.time())


class Timer:
    """`with Timer() as t: ...; t.seconds` — monotonic wall-clock duration."""

    def __enter__(self):
        self._start = time.monotonic()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc):
        self.seconds = time.monotonic() - self._start
        return False
