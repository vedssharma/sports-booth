import asyncio

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from booth import orchestrator, pipeline
from booth.budget import WINDOW_S, BudgetGuard
from booth.policy import CommentaryPolicy

MAIN, FAST = "main-model", "fast-model"


def make_policy(cap=10.0):
    now = [0.0]
    budget = BudgetGuard(cap, clock=lambda: now[0])
    return CommentaryPolicy(budget, MAIN, FAST), budget, now


def ev(etype, **kw):
    return {"type": etype, "game_id": "G1", **kw}


# ── Budget guard ──────────────────────────────────────────────────────────────

def test_budget_states_and_rolling_window():
    now = [0.0]
    b = BudgetGuard(10, clock=lambda: now[0])
    assert b.state() == "ok"
    b.record(7.5); assert b.state() == "saver"
    b.record(2.5); assert b.state() == "exhausted"
    now[0] = WINDOW_S + 1          # spend rolls out of the window
    assert b.state() == "ok" and b.spent() == 0


def test_cap_of_zero_means_unlimited():
    b = BudgetGuard(0)
    b.record(1000)
    assert b.state() == "ok"


def test_transition_reports_each_change_once():
    b = BudgetGuard(10)
    assert b.transition() is None
    b.record(8)
    assert b.transition() == ("ok", "saver") and b.transition() is None


# ── Policy ────────────────────────────────────────────────────────────────────

def test_big_moments_get_full_booth_on_main_model():
    policy, _, _ = make_policy()
    for t in ("scoring_run", "close_game", "game_final", "buzzer_beater"):  # unknown types are big too
        d = policy.decide(ev(t))
        assert d.model == MAIN and len(d.agents) == 3


def test_new_event_types_policy():
    policy, budget, _ = make_policy(10)
    for t in ("lead_change", "player_milestone"):
        d = policy.decide(ev(t))
        assert d.model == MAIN and len(d.agents) == 3
    d = policy.decide(ev("foul_trouble"))
    assert d.model == FAST and d.agents == ("analyst", "degenerate")
    budget.record(8)   # saver mode: foul trouble is low value and skipped, milestones still covered
    assert policy.decide(ev("foul_trouble")) is None
    assert policy.decide(ev("player_milestone")).model == FAST


def test_quarter_start_and_join_use_cheap_model_full_booth():
    policy, _, _ = make_policy()
    assert policy.decide(ev("quarter_start")).model == FAST
    d = policy.decide(ev("game_update", join=True))
    assert d.model == FAST and len(d.agents) == 3


def test_routine_update_rotates_a_single_persona_per_game():
    policy, _, _ = make_policy()
    seen = [policy.decide(ev("game_update")).agents for _ in range(4)]
    assert all(len(a) == 1 for a in seen)
    assert [a[0] for a in seen] == ["analyst", "historian", "degenerate", "analyst"]
    other = policy.decide({"type": "game_update", "game_id": "G2"}).agents
    assert other == ("analyst",)   # rotation is per game


def test_saver_mode_drops_routine_and_downgrades_model():
    policy, budget, _ = make_policy(10)
    budget.record(8)
    assert policy.decide(ev("game_update")) is None
    d = policy.decide(ev("scoring_run"))
    assert d.model == FAST and len(d.agents) == 3


def test_exhausted_only_allows_finals():
    policy, budget, _ = make_policy(10)
    budget.record(11)
    assert policy.decide(ev("scoring_run")) is None
    assert policy.decide(ev("quarter_start")) is None
    assert policy.decide(ev("game_final")).model == FAST


# ── Orchestrator (SDK query mocked) ───────────────────────────────────────────

def fake_query(calls, cost=0.01):
    async def query(prompt, options):
        calls.append(options)
        yield AssistantMessage(content=[TextBlock(text=f"take from {options.model}")], model=options.model)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id="s", total_cost_usd=cost)
    return query


def test_orchestrator_runs_only_requested_agents_and_sums_cost(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator, "query", fake_query(calls, cost=0.02))
    out = asyncio.run(orchestrator.run_booth_commentary(
        {"type": "game_update"}, model="m1", agents=("historian", "degenerate")))
    assert {c.model for c in calls} == {"m1"} and len(calls) == 2
    assert "analyst" not in out and out["historian"] == "take from m1"
    assert out["cost_usd"] == 0.04 and out["model"] == "m1"
    assert all(c.max_budget_usd == orchestrator.MAX_USD_PER_AGENT for c in calls)


def test_orchestrator_isolates_agent_failures(monkeypatch):
    async def query(prompt, options):
        if options.system_prompt.startswith("You are The Historian"):
            raise RuntimeError("boom")
        yield AssistantMessage(content=[TextBlock(text="ok")], model="m")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id="s", total_cost_usd=0.01)
    monkeypatch.setattr(orchestrator, "query", query)
    out = asyncio.run(orchestrator.run_booth_commentary({"type": "scoring_run"}))
    assert out["historian"] == "[Error: boom]" and out["analyst"] == "ok"
    assert out["cost_usd"] == 0.02   # failed run contributes no cost


# ── process_event wiring ──────────────────────────────────────────────────────

def test_process_event_records_spend_stores_history_and_skips_when_exhausted(monkeypatch):
    sent, stored = [], []
    now = [0.0]
    budget = BudgetGuard(1.0, clock=lambda: now[0])
    monkeypatch.setattr(pipeline, "budget", budget)
    monkeypatch.setattr(pipeline, "policy", CommentaryPolicy(budget, MAIN, FAST))
    monkeypatch.setattr(pipeline.history, "add", stored.append)

    async def fake_manager_broadcast(payload):
        sent.append(payload)
    monkeypatch.setattr(pipeline.manager, "broadcast", fake_manager_broadcast)
    monkeypatch.setattr(orchestrator, "query", fake_query([], cost=0.6))

    async def go():
        await pipeline.process_event(ev("scoring_run", event="run"))       # spends 3 x 0.6 = 1.8
        await pipeline.process_event(ev("scoring_run", event="run 2"))     # budget exhausted -> skipped
    asyncio.run(go())

    kinds = [m["type"] for m in sent]
    assert kinds.count("commentary") == 1 and len(stored) == 1
    event_msg = next(m for m in sent if m["type"] == "event")
    assert sorted(event_msg["data"]["agents"]) == ["analyst", "degenerate", "historian"]
    assert any(m["type"] == "status" and "paused" in m["message"] for m in sent)
    assert budget.state() == "exhausted"


# ── Booth continuity (earlier commentary in the prompt) ───────────────────────

def _moment(label, **takes):
    return {"event": {"event": label, "game_id": "G1"}, **takes}


def test_prompt_without_history_is_just_the_event():
    p = orchestrator.build_prompt({"type": "scoring_run", "event": "BOS on a 9-0 run"})
    assert "Earlier booth commentary" not in p and "BOS on a 9-0 run" in p


def test_prompt_includes_recent_moments_clipped_and_without_charts():
    long_take = "x" * 500
    earlier = [
        _moment("old moment", analyst="should be dropped by the 3-moment window"),
        _moment("m1", analyst="Analyst point one. [CHART]{\"type\":\"stat_bars\"}[/CHART]", historian="h1"),
        _moment("m2", degenerate=long_take),
        _moment("m3", analyst="a3"),
    ]
    p = orchestrator.build_prompt({"event": "now"}, earlier)
    assert "old moment" not in p and "* m1" in p and "* m3" in p
    assert "ANALYST: Analyst point one." in p and "[CHART]" not in p
    assert "DEGENERATE: " + "x" * 100 in p and "x" * 300 not in p and "…" in p
    assert "context only" in p and p.index("Earlier booth commentary") < p.index("Game event:")


def test_run_booth_commentary_passes_context_to_every_agent(monkeypatch):
    prompts = []

    async def query(prompt, options):
        prompts.append(prompt)
        yield AssistantMessage(content=[TextBlock(text="ok")], model="m")

    monkeypatch.setattr(orchestrator, "query", query)
    asyncio.run(orchestrator.run_booth_commentary(
        {"event": "e"}, earlier=[_moment("prior", analyst="earlier take")]))
    assert len(prompts) == 3 and all("earlier take" in p for p in prompts)


def test_process_event_feeds_game_history_into_the_next_event(monkeypatch):
    from booth.history import HistoryStore
    store = HistoryStore(":memory:")
    monkeypatch.setattr(pipeline, "history", store)
    seen = []

    async def query(prompt, options):
        seen.append(prompt)
        yield AssistantMessage(content=[TextBlock(text=f"take {len(seen)}")], model="m")

    async def quiet(_):
        pass
    monkeypatch.setattr(orchestrator, "query", query)
    monkeypatch.setattr(pipeline.manager, "broadcast", quiet)
    other = {"type": "scoring_run", "game_id": "G2", "event": "other game"}
    first = {"type": "scoring_run", "game_id": "G1", "event": "first moment"}
    second = {"type": "scoring_run", "game_id": "G1", "event": "second moment"}

    async def go():
        await pipeline.process_event(other, cli_only=True)
        await pipeline.process_event(first, cli_only=True)
        n = len(seen)
        await pipeline.process_event(second, cli_only=True)
        return seen[n:]

    second_prompts = asyncio.run(go())
    assert all("* first moment" in p for p in second_prompts)
    assert not any("other game" in p for p in second_prompts)   # other games never leak in


def test_history_recent_for_game_is_ordered_and_scoped():
    from booth.history import HistoryStore
    store = HistoryStore(":memory:")
    for i in range(5):
        store.add({"event": {"game_id": "A", "event": f"a{i}"}})
    store.add({"event": {"game_id": "B", "event": "b0"}})
    assert [m["event"]["event"] for m in store.recent_for_game("A", 3)] == ["a2", "a3", "a4"]
    assert store.recent_for_game("none") == []
