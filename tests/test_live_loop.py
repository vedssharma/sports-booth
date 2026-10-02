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


def test_live_loop_adds_player_events_from_box_scores(monkeypatch):
    from tests.test_players import box, player

    polls = [[game(20, 20)], [game(24, 22)]]
    boxes = [box(home=[player("Tatum", 1, pts=18)]), box(home=[player("Tatum", 1, pts=21)])]
    state = {"i": 0}
    handled = []

    async def fake_fetch():
        i = state["i"]; state["i"] += 1
        if i >= len(polls):
            raise asyncio.CancelledError
        return polls[i]

    async def fake_boxes(ids):
        return {gid: boxes[state["i"] - 1] for gid in ids}

    async def handler(event):
        handled.append(event["type"])

    real_sleep = asyncio.sleep
    monkeypatch.setattr(live, "fetch_started_games", fake_fetch)
    monkeypatch.setattr(live, "fetch_boxscores", fake_boxes)
    monkeypatch.setattr(live.asyncio, "sleep", lambda _: real_sleep(0))

    async def go():
        scheduler = EventScheduler(handler, max_pending=5)
        try:
            await live.live_loop(1, True, scheduler)
        except asyncio.CancelledError:
            pass
        await scheduler.join()

    asyncio.run(go())
    assert "player_milestone" in handled


def test_box_score_failure_does_not_break_polling(monkeypatch):
    polls = [[game(20, 20)], [game(24, 22)]]
    state = {"i": 0}

    async def fake_fetch():
        i = state["i"]; state["i"] += 1
        if i >= len(polls):
            raise asyncio.CancelledError
        return polls[i]

    async def no_boxes(ids):
        return {}   # every box score fetch failed

    real_sleep = asyncio.sleep
    monkeypatch.setattr(live, "fetch_started_games", fake_fetch)
    monkeypatch.setattr(live, "fetch_boxscores", no_boxes)
    monkeypatch.setattr(live.asyncio, "sleep", lambda _: real_sleep(0))

    async def go():
        scheduler = EventScheduler(lambda e: asyncio.sleep(0), max_pending=5)
        try:
            await live.live_loop(1, True, scheduler)
        except asyncio.CancelledError:
            pass
        await scheduler.join()
        return scheduler.stats["processed"]

    assert asyncio.run(go()) >= 1
