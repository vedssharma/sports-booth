import asyncio
import io
import json
import logging
import time

import pytest
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage,
)
from fastapi.testclient import TestClient

from booth import health, live, log, metrics, orchestrator, pipeline, server
from booth.budget import BudgetGuard
from booth.policy import CommentaryPolicy
from booth.scheduler import EventScheduler
from booth.security import Auth
from tests.test_events import game

reg = metrics.registry


# ── Logging ───────────────────────────────────────────────────────────────────

def capture(fmt):
    stream = io.StringIO()
    log.setup_logging("DEBUG", fmt, stream)
    return stream


def test_json_logs_are_one_object_per_line_with_extras():
    stream = capture("json")
    log.get("t").info("hello", extra={"persona": "analyst", "cost_usd": 0.01})
    log.get("t").warning("careful")
    first, second = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert first["msg"] == "hello" and first["level"] == "info" and first["logger"] == "booth.t"
    assert first["persona"] == "analyst" and first["cost_usd"] == 0.01 and first["ts"].endswith("+00:00")
    assert second["level"] == "warning"


def test_text_logs_are_readable_and_show_extras():
    stream = capture("text")
    log.get("t").info("hello", extra={"persona": "analyst"})
    log.get("t").error("boom")
    out = stream.getvalue()
    assert "hello  [persona=analyst]" in out and "✗  boom" in out


def test_exceptions_are_included():
    stream = capture("json")
    try:
        raise ValueError("bad")
    except ValueError:
        log.get("t").exception("failed")
    assert "ValueError: bad" in json.loads(stream.getvalue())["exc"]


def test_bound_context_reaches_nested_tasks_and_does_not_leak():
    stream = capture("json")

    async def worker(n):
        log.get("t").info(f"in task {n}")

    async def go():
        with log.bind(event_id="abc123", game_id="G1"):
            await asyncio.gather(worker(1), worker(2))      # gather copies the context into each task
        log.get("t").info("outside")

    asyncio.run(go())
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [r.get("event_id") for r in rows] == ["abc123", "abc123", None]
    assert rows[0]["game_id"] == "G1" and "game_id" not in rows[2]


def test_setup_logging_is_idempotent_and_respects_level():
    capture("json")
    stream = capture("json")
    log.setup_logging("WARNING", "json", stream)
    log.get("t").info("hidden")
    log.get("t").warning("shown")
    assert len(stream.getvalue().splitlines()) == 1
    assert len(logging.getLogger("booth").handlers) == 1


# ── Metrics registry ──────────────────────────────────────────────────────────

def test_counters_gauges_and_labels():
    reg.inc("events_submitted_total", type="scoring_run")
    reg.inc("events_submitted_total", 2, type="scoring_run")
    reg.inc("events_submitted_total", type="close_game")
    reg.set("websocket_clients", 3)
    assert reg.value("events_submitted_total", type="scoring_run") == 3
    assert reg.total("events_submitted_total") == 4 and reg.value("websocket_clients") == 3


def test_undeclared_or_mistyped_metrics_fail_loudly():
    with pytest.raises(KeyError):
        reg.inc("events_submited_total")                   # typo
    with pytest.raises(TypeError):
        reg.set("events_submitted_total", 1)               # counter used as a gauge


def test_summary_quantiles_and_json_snapshot():
    for v in range(1, 101):
        reg.observe("agent_run_seconds", float(v), persona="analyst")
    row = reg.snapshot()["agent_run_seconds"][0]
    assert row["labels"] == {"persona": "analyst"} and row["count"] == 100 and row["sum"] == 5050.0
    assert row["p50"] == 51.0 and row["p95"] == 96.0


def test_prometheus_exposition_format():
    reg.inc("agent_runs_total", persona="analyst", model='m"1', outcome="ok")
    reg.observe("event_queue_wait_seconds", 2.0)
    reg.observe("event_queue_wait_seconds", 4.0)
    reg.set("budget_cap_usd", 5)
    text = reg.render_prometheus()
    assert "# HELP booth_agent_runs_total" in text and "# TYPE booth_agent_runs_total counter" in text
    assert 'booth_agent_runs_total{model="m\\"1",outcome="ok",persona="analyst"} 1' in text   # escaped quote
    assert "# TYPE booth_event_queue_wait_seconds summary" in text
    assert 'booth_event_queue_wait_seconds{quantile="0.5"} 4' in text
    assert "booth_event_queue_wait_seconds_sum 6" in text and "booth_event_queue_wait_seconds_count 2" in text
    assert "booth_budget_cap_usd 5" in text and text.endswith("\n")


def test_reservoir_is_bounded():
    for i in range(metrics.RESERVOIR * 2):
        reg.observe("agent_run_seconds", 1.0, persona="x")
    assert len(reg._samples["agent_run_seconds"][(("persona", "x"),)]["recent"]) == metrics.RESERVOIR
    assert reg.snapshot()["agent_run_seconds"][0]["count"] == metrics.RESERVOIR * 2


# ── Scheduler metrics ─────────────────────────────────────────────────────────

def test_scheduler_records_drops_outcomes_and_waits():
    async def go():
        gate = asyncio.Event()

        async def handler(event):
            await gate.wait()
            if event["event"] == "boom":
                raise RuntimeError("x")

        s = EventScheduler(handler, max_pending=5)
        s.submit({"game_id": "A", "type": "quarter_start", "event": "in-flight"})
        await asyncio.sleep(0.01)
        s.submit({"game_id": "A", "type": "game_update", "event": "busy"})          # dropped: busy
        s.submit({"game_id": "A", "type": "scoring_run", "event": "old"})
        s.submit({"game_id": "A", "type": "scoring_run", "event": "new"})           # supersedes "old"
        s.submit({"game_id": "A", "type": "game_final", "event": "boom"})
        gate.set()
        await s.join()

    asyncio.run(go())
    assert reg.value("events_submitted_total", type="scoring_run") == 2
    assert reg.value("events_dropped_total", reason="busy") == 1
    assert reg.value("events_dropped_total", reason="superseded") == 1
    assert reg.value("events_processed_total", type="quarter_start", outcome="ok") == 1
    assert reg.value("events_processed_total", type="game_final", outcome="error") == 1
    snap = reg.snapshot()
    assert snap["event_queue_wait_seconds"][0]["count"] == 3        # in-flight, new, boom
    assert {r["labels"]["type"] for r in snap["event_process_seconds"]} == {"quarter_start", "scoring_run", "game_final"}


# ── Agent metrics ─────────────────────────────────────────────────────────────

async def stream(*messages):
    for m in messages:
        yield m


def usage_result(cost=0.02):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                         session_id="s", total_cost_usd=cost,
                         usage={"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": 700})


def test_collect_counts_tool_calls_unavailable_results_and_tokens():
    messages = [
        AssistantMessage(content=[ToolUseBlock(id="t1", name="get_boxscore", input={})], model="m"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content='{"error": "NBA boxscore data unavailable: x"}')]),
        AssistantMessage(content=[ToolUseBlock(id="t2", name="get_recent_plays", input={})], model="m"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t2", content=[{"type": "text", "text": "[]"}])]),
        AssistantMessage(content=[TextBlock(text="done")], model="m"),
        usage_result(),
    ]
    text, cost = asyncio.run(orchestrator._collect(stream(*messages), "analyst"))
    assert (text, cost) == ("done", 0.02)
    assert reg.value("agent_tool_calls_total", persona="analyst", tool="get_boxscore") == 1
    assert reg.value("agent_tool_calls_total", persona="analyst", tool="get_recent_plays") == 1
    assert reg.value("agent_tool_unavailable_total", persona="analyst", tool="get_boxscore") == 1
    assert reg.total("agent_tool_unavailable_total") == 1            # the healthy result isn't counted
    assert reg.value("agent_tokens_total", persona="analyst", kind="input") == 100
    assert reg.value("agent_tokens_total", persona="analyst", kind="output") == 40
    assert reg.value("agent_tokens_total", persona="analyst", kind="cache_read") == 700
    assert reg.value("agent_tokens_total", persona="analyst", kind="cache_creation") == 0


def test_tool_error_results_count_as_unavailable():
    messages = [AssistantMessage(content=[ToolUseBlock(id="t1", name="get_live_odds", input={})], model="m"),
                UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="boom", is_error=True)]),
                AssistantMessage(content=[TextBlock(text="x")], model="m")]
    asyncio.run(orchestrator._collect(stream(*messages), "degenerate"))
    assert reg.value("agent_tool_unavailable_total", persona="degenerate", tool="get_live_odds") == 1


def test_run_agent_records_success_and_failure(monkeypatch):
    async def ok(prompt, options):
        yield AssistantMessage(content=[TextBlock(text="fine")], model="m")
        yield usage_result(0.05)

    async def broken(prompt, options):
        raise RuntimeError("cli died")
        yield  # pragma: no cover

    monkeypatch.setattr(orchestrator, "query", ok)
    asyncio.run(orchestrator._run_agent("historian", "e", "model-a"))
    monkeypatch.setattr(orchestrator, "query", broken)
    with pytest.raises(RuntimeError):
        asyncio.run(orchestrator._run_agent("historian", "e", "model-a"))
    assert reg.value("agent_runs_total", persona="historian", model="model-a", outcome="ok") == 1
    assert reg.value("agent_runs_total", persona="historian", model="model-a", outcome="error") == 1
    assert reg.value("agent_cost_usd_total", persona="historian", model="model-a") == 0.05
    assert reg.snapshot()["agent_run_seconds"][0]["count"] == 2     # failures are timed too


# ── Pipeline: budget skips and log correlation ────────────────────────────────

def test_pipeline_counts_budget_skips_and_logs_with_event_context(monkeypatch):
    budget = BudgetGuard(1.0)
    budget.record(5)
    monkeypatch.setattr(pipeline, "budget", budget)
    monkeypatch.setattr(pipeline, "policy", CommentaryPolicy(budget, "m", "f"))

    async def quiet(_):
        pass
    monkeypatch.setattr(pipeline.manager, "broadcast", quiet)
    stream_ = capture("json")
    asyncio.run(pipeline.process_event({"type": "scoring_run", "game_id": "G7", "event": "run"}))
    assert reg.value("events_skipped_budget_total", type="scoring_run") == 1
    rows = [json.loads(line) for line in stream_.getvalue().splitlines()]
    assert rows and all(r["game_id"] == "G7" and len(r["event_id"]) == 8 for r in rows)
    assert len({r["event_id"] for r in rows}) == 1                  # one id ties the whole event together
    assert any("budget" in r["msg"] for r in rows)


# ── Health ────────────────────────────────────────────────────────────────────

def report(rt, budget=None, **kw):
    return rt.report(clients=0, scheduler=None, budget=budget or {"state": "ok"}, **kw)


def test_health_ok_by_default_and_reports_version():
    rt = health.RuntimeState(mode="live")
    r = report(rt)
    assert r["status"] == "ok" and r["reasons"] == [] and r["version"] == "0.1.0" and r["mode"] == "live"
    assert r["polling"] == {"interval_s": 45.0, "consecutive_failures": 0, "last_success_age_s": None}


def test_repeated_poll_failures_degrade_health_and_recovery_clears_it():
    rt = health.RuntimeState(mode="live")
    for _ in range(health.FAILED_POLLS_BEFORE_DEGRADED - 1):
        rt.poll_failed()
    assert report(rt)["status"] == "ok"
    rt.poll_failed()
    r = report(rt)
    assert r["status"] == "degraded" and "failed 3 times" in r["reasons"][0]
    rt.poll_ok()
    assert report(rt)["status"] == "ok" and reg.value("scoreboard_polls_total", outcome="error") == 3


def test_stale_polling_degrades_health():
    rt = health.RuntimeState(mode="live", poll_interval_s=10)
    rt.poll_ok()
    rt.last_poll_ok_at = time.time() - 500
    r = report(rt)
    assert r["status"] == "degraded" and "no successful scoreboard poll for" in r["reasons"][0]


def test_exhausted_budget_degrades_health_and_demo_mode_ignores_polling():
    rt = health.RuntimeState(mode="demo")
    rt.consecutive_poll_failures = 99
    r = report(rt, budget={"state": "exhausted"})
    assert r["status"] == "degraded" and len(r["reasons"]) == 1 and "budget" in r["reasons"][0]
    assert r["polling"] is None


def test_health_summarises_agents():
    reg.inc("agent_runs_total", 3, persona="analyst", model="m", outcome="ok")
    reg.inc("agent_runs_total", persona="analyst", model="m", outcome="error")
    reg.observe("agent_run_seconds", 4.0, persona="analyst")
    reg.inc("agent_tool_calls_total", 5, persona="analyst", tool="t")
    reg.inc("agent_tool_unavailable_total", persona="analyst", tool="t")
    agents = report(health.RuntimeState(mode="live"))["agents"]
    assert agents["runs"] == {"analyst": 4} and agents["errors"] == {"analyst": 1}
    assert agents["latency"]["analyst"]["count"] == 1 and agents["tool_calls"] == 5 and agents["tool_unavailable"] == 1


# ── Live loop feeds health ────────────────────────────────────────────────────

def test_live_loop_tracks_poll_outcomes(monkeypatch):
    calls = {"i": 0}

    async def fake_fetch():
        calls["i"] += 1
        if calls["i"] <= 2:
            raise ConnectionError("nba down")
        if calls["i"] > 3:
            raise asyncio.CancelledError
        return [game(10, 8)]

    async def no_boxes(ids):
        return {}

    real_sleep = asyncio.sleep
    monkeypatch.setattr(live, "fetch_started_games", fake_fetch)
    monkeypatch.setattr(live, "fetch_boxscores", no_boxes)
    monkeypatch.setattr(live.asyncio, "sleep", lambda _: real_sleep(0))

    async def go():
        s = EventScheduler(lambda e: asyncio.sleep(0))
        try:
            await live.live_loop(1, True, s)
        except asyncio.CancelledError:
            pass
        await s.join()

    asyncio.run(go())
    assert reg.value("scoreboard_polls_total", outcome="error") == 2
    assert reg.value("scoreboard_polls_total", outcome="ok") == 1
    assert health.runtime.consecutive_poll_failures == 0 and health.runtime.last_poll_ok_at is not None


# ── Endpoints ─────────────────────────────────────────────────────────────────

def test_health_and_metrics_endpoints():
    reg.inc("events_submitted_total", type="scoring_run")
    health.runtime.mode = "live"
    with TestClient(server.app) as c:
        h = c.get("/health").json()
        m = c.get("/metrics")
    assert h["status"] in ("ok", "degraded") and h["mode"] == "live" and "budget" in h and "agents" in h
    assert m.status_code == 200 and m.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'booth_events_submitted_total{type="scoring_run"} 1' in m.text
    assert "booth_budget_cap_usd" in m.text and "booth_websocket_clients 0" in m.text


def test_metrics_requires_the_token_when_auth_is_enabled(monkeypatch):
    monkeypatch.setattr(server.app.state, "auth", Auth("t" * 24))
    with TestClient(server.app) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"authorization": "Bearer " + "t" * 24}).status_code == 200
