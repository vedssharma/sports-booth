import asyncio

from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock, ToolUseBlock

from booth import orchestrator, pipeline
from booth.server import ConnectionManager
from booth.streaming import StreamRelay


def delta(text):
    return StreamEvent(uuid="u", session_id="s", event={
        "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})


def tool_start():
    return StreamEvent(uuid="u", session_id="s", event={
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "id": "t1", "name": "get_boxscore", "input": {}}})


def result(cost=0.01):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                         num_turns=2, session_id="s", total_cost_usd=cost)


async def stream(*messages):
    for m in messages:
        yield m


def collect(messages, role="analyst"):
    got = []
    text, cost = asyncio.run(orchestrator._collect(
        stream(*messages), role, lambda r, kind, t: got.append((r, kind, t))))
    return text, cost, got


# ── _collect ──────────────────────────────────────────────────────────────────

def test_deltas_are_forwarded_and_final_text_comes_from_the_assistant_message():
    text, cost, got = collect([delta("Hel"), delta("lo"),
                               AssistantMessage(content=[TextBlock(text="Hello")], model="m"), result(0.03)])
    assert got == [("analyst", "delta", "Hel"), ("analyst", "delta", "lo")]
    assert text == "Hello" and cost == 0.03


def test_preamble_before_a_tool_call_is_reset_and_excluded_from_final_text():
    text, _, got = collect([
        delta("Let me check the box score."), tool_start(),
        AssistantMessage(content=[TextBlock(text="Let me check the box score."),
                                  ToolUseBlock(id="t1", name="get_boxscore", input={})], model="m"),
        delta("Real take."),
        AssistantMessage(content=[TextBlock(text="Real take.")], model="m"), result()])
    assert [k for _, k, _ in got] == ["delta", "reset", "delta"]
    assert text == "Real take."


def test_falls_back_to_all_text_when_every_turn_used_a_tool():
    text, _, _ = collect([AssistantMessage(content=[TextBlock(text="partial thoughts"),
                                                    ToolUseBlock(id="t", name="x", input={})], model="m"), result()])
    assert text == "partial thoughts"


def test_streaming_is_off_unless_requested(monkeypatch):
    seen = []

    async def query(prompt, options):
        seen.append(options.include_partial_messages)
        yield AssistantMessage(content=[TextBlock(text="ok")], model="m")

    monkeypatch.setattr(orchestrator, "query", query)
    asyncio.run(orchestrator.run_booth_commentary({"event": "e"}, agents=("analyst",)))
    asyncio.run(orchestrator.run_booth_commentary({"event": "e"}, agents=("analyst",), on_stream=lambda *a: None))
    assert seen == [False, True]


# ── Relay ─────────────────────────────────────────────────────────────────────

class RecordingManager(ConnectionManager):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def broadcast(self, payload):
        self.sent.append(payload)


def test_relay_coalesces_tokens_and_tracks_in_progress_text():
    async def go():
        m = RecordingManager()
        relay = StreamRelay(m, "G1", interval=0.02)
        for tok in ("The ", "Celtics ", "are ", "hot."):
            relay("analyst", "delta", tok)
        relay("historian", "delta", "History.")
        await asyncio.sleep(0.08)
        assert m.streaming == {"G1": {"analyst": "The Celtics are hot.", "historian": "History."}}
        assert m.snapshot()["streaming"] == m.streaming       # late joiners get the text so far
        relay("analyst", "reset", "")
        relay("analyst", "delta", "Fresh.")
        await asyncio.sleep(0.08)
        await relay.close()
        return m
    m = asyncio.run(go())
    deltas = [p for p in m.sent if p["type"] == "delta" and p["role"] == "analyst"]
    assert [d["text"] for d in deltas] == ["The Celtics are hot.", "Fresh."]   # 4 tokens -> 1 message
    assert [d["reset"] for d in deltas] == [False, True]
    assert m.streaming == {}


def test_close_discards_unsent_text():
    async def go():
        m = RecordingManager()
        relay = StreamRelay(m, "G1", interval=10)
        relay("analyst", "delta", "never sent")
        await relay.close()
        return m
    assert asyncio.run(go()).sent == []


# ── process_event ─────────────────────────────────────────────────────────────

def test_process_event_streams_before_the_final_commentary(monkeypatch):
    from booth.history import HistoryStore
    sent = []
    mgr = RecordingManager()
    mgr.broadcast = lambda payload: _record(sent, payload)
    monkeypatch.setattr(pipeline, "manager", mgr)
    monkeypatch.setattr(pipeline, "history", HistoryStore(":memory:"))
    monkeypatch.setattr("booth.streaming.FLUSH_INTERVAL_S", 0.01)

    async def query(prompt, options):
        yield delta("Live ")
        await asyncio.sleep(0.05)       # give the relay time to flush
        yield delta("take.")
        await asyncio.sleep(0.05)
        yield AssistantMessage(content=[TextBlock(text="Live take.")], model="m")
        yield result()

    monkeypatch.setattr(orchestrator, "query", query)
    asyncio.run(pipeline.process_event(
        {"type": "foul_trouble", "game_id": "G1", "event": "A in foul trouble"}))
    kinds = [m["type"] for m in sent]
    assert kinds[0] == "event" and kinds[-1] == "commentary" and "delta" in kinds
    assert kinds.index("delta") < kinds.index("commentary")
    assert mgr.streaming == {}          # cleared once the finished text is out


async def _record(sent, payload):
    sent.append(payload)
