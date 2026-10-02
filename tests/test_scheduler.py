import asyncio

from booth.scheduler import EventScheduler


def ev(gid="A", etype="scoring_run", label=""):
    return {"game_id": gid, "type": etype, "event": label or etype}


class Harness:
    """Handler that records start/finish order and blocks until released."""

    def __init__(self):
        self.started, self.finished = [], []
        self.gate = asyncio.Event()
        self.running = 0
        self.max_running = 0

    async def handler(self, event):
        self.started.append(event["event"])
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        await self.gate.wait()
        self.running -= 1
        self.finished.append(event["event"])


def run(coro):
    return asyncio.run(coro)


def test_events_for_one_game_run_in_order():
    async def go():
        h = Harness(); h.gate.set()
        s = EventScheduler(h.handler)
        for i in range(3):
            s.submit(ev(etype="quarter_start", label=f"e{i}"))
        await s.join()
        return h.finished
    # max_pending=2 keeps the 2 newest pending plus the one already picked up
    assert run(go())[-1] == "e2"


def test_concurrency_is_capped_across_games():
    async def go():
        h = Harness()
        s = EventScheduler(h.handler, max_concurrent=2)
        for g in "ABCD":
            s.submit(ev(g, "scoring_run", g))
        await asyncio.sleep(0.05)
        assert h.running == 2
        h.gate.set()
        await s.join()
        return h
    h = run(go())
    assert h.max_running == 2 and sorted(h.finished) == list("ABCD")


def test_routine_update_dropped_while_game_busy():
    async def go():
        h = Harness()
        s = EventScheduler(h.handler)
        assert s.submit(ev("A", "scoring_run", "busy"))
        await asyncio.sleep(0.01)
        assert s.submit(ev("A", "game_update", "routine")) is False
        h.gate.set(); await s.join()
        return s.stats, h.finished
    stats, finished = run(go())
    assert stats["dropped_busy"] == 1 and finished == ["busy"]


def test_newer_event_of_same_type_supersedes_pending():
    async def go():
        h = Harness()
        s = EventScheduler(h.handler)
        s.submit(ev("A", "quarter_start", "in-flight"))
        await asyncio.sleep(0.01)
        s.submit(ev("A", "scoring_run", "old run"))
        s.submit(ev("A", "scoring_run", "new run"))
        h.gate.set(); await s.join()
        return h.finished, s.stats
    finished, stats = run(go())
    assert finished == ["in-flight", "new run"] and stats["dropped_superseded"] == 1


def test_overflow_drops_lowest_priority_but_never_final():
    async def go():
        h = Harness()
        s = EventScheduler(h.handler, max_pending=2)
        s.submit(ev("A", "quarter_start", "in-flight"))
        await asyncio.sleep(0.01)
        s.submit(ev("A", "close_game", "close"))
        s.submit(ev("A", "game_final", "final"))
        s.submit(ev("A", "quarter_start", "q"))   # 3 pending -> drop the lowest-priority oldest
        h.gate.set(); await s.join()
        return h.finished
    finished = run(go())
    assert "final" in finished and "close" not in finished


def test_stale_events_are_skipped_but_final_is_kept():
    async def go():
        now = [0.0]
        h = Harness()
        s = EventScheduler(h.handler, stale_after_s=100, clock=lambda: now[0])
        s.submit(ev("A", "quarter_start", "in-flight"))
        await asyncio.sleep(0.01)
        s.submit(ev("A", "scoring_run", "stale run"))
        s.submit(ev("A", "game_final", "final"))
        now[0] = 500
        h.gate.set(); await s.join()
        return h.finished, s.stats
    finished, stats = run(go())
    assert finished == ["in-flight", "final"] and stats["dropped_stale"] == 1


def test_handler_failure_does_not_stop_worker():
    async def go():
        seen = []
        async def handler(event):
            seen.append(event["event"])
            if event["event"] == "boom":
                raise RuntimeError("x")
        s = EventScheduler(handler, max_pending=5)
        s.submit(ev("A", "quarter_start", "boom"))
        s.submit(ev("A", "game_final", "after"))
        await s.join()
        return seen, s.stats
    seen, stats = run(go())
    assert seen == ["boom", "after"] and stats["failed"] == 1
