import asyncio

from booth import live
from booth.scheduler import EventScheduler
from tests.test_events import game


def test_live_loop_submits_events_without_waiting_on_commentary(monkeypatch):
    """A slow handler must not stall polling: all polls complete before any handler finishes."""
    polls = [[game(20, 20)], [game(30, 20)], [game(30, 20, 4, "Final", game_status=3)]]
    state = {"i": 0, "handled": []}
    release = asyncio.Event()

    async def fake_fetch():
        i = state["i"]; state["i"] += 1
        if i >= len(polls):
            raise asyncio.CancelledError
        return polls[i]

    async def slow_handler(event):
        await release.wait()
        state["handled"].append(event["type"])

    real_sleep = asyncio.sleep
    monkeypatch.setattr(live, "fetch_started_games", fake_fetch)
    monkeypatch.setattr(live.asyncio, "sleep", lambda _: real_sleep(0))

    async def go():
        scheduler = EventScheduler(slow_handler, max_pending=5)
        try:
            await live.live_loop(1, True, scheduler)
        except asyncio.CancelledError:
            pass
        assert state["handled"] == []      # polling finished while commentary is still pending
        release.set()
        await scheduler.join()
        return state["handled"]

    assert asyncio.run(go()) == ["game_update", "scoring_run", "game_final"]
