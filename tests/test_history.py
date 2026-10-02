import asyncio

from fastapi.testclient import TestClient

from booth import server
from booth.history import KEEP_PER_GAME, MAX_AGE_S, HistoryStore


def item(gid="G1", label="x", n=0):
    return {"event": {"game_id": gid, "event": label}, "analyst": f"a{n}", "historian": "h", "degenerate": "d"}


def test_recent_returns_oldest_first_and_persists_across_instances(tmp_path):
    path = tmp_path / "h.db"
    store = HistoryStore(path)
    for n in range(3):
        store.add(item(n=n))
    reopened = HistoryStore(path)  # simulates a server restart
    assert [m["analyst"] for m in reopened.recent()] == ["a0", "a1", "a2"]


def test_per_game_limit_on_read_and_retention_on_write():
    store = HistoryStore(":memory:")
    for n in range(KEEP_PER_GAME + 10):
        store.add(item("A", n=n))
    store.add(item("B", n=0))
    recent_a = [m for m in store.recent(per_game=5) if m["event"]["game_id"] == "A"]
    assert [m["analyst"] for m in recent_a] == [f"a{n}" for n in range(KEEP_PER_GAME + 5, KEEP_PER_GAME + 10)]
    assert len(store._db.execute("SELECT id FROM moments WHERE game_id='A'").fetchall()) == KEEP_PER_GAME


def test_old_items_expire():
    store = HistoryStore(":memory:")
    store.add(item(n=0), now=1000)
    store.add(item(n=1), now=1000 + MAX_AGE_S + 5)   # also prunes the old row
    assert [m["analyst"] for m in store.recent(now=1000 + MAX_AGE_S + 6)] == ["a1"]


def test_late_joiner_gets_snapshot_with_games_and_history(monkeypatch):
    store = HistoryStore(":memory:")
    store.add(item("G1", "Run!", 1))
    monkeypatch.setattr(server, "history", store)
    mgr = server.ConnectionManager()
    monkeypatch.setattr(server, "manager", mgr)
    asyncio.run(mgr.broadcast({"type": "games", "data": [{"gameId": "G1"}]}))

    with TestClient(server.app) as client, client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
    assert snap["type"] == "snapshot"
    assert snap["games"] == [{"gameId": "G1"}] and snap["status"] is None
    assert snap["history"][0]["event"]["event"] == "Run!"


def test_snapshot_reports_status_when_no_games():
    mgr = server.ConnectionManager()
    asyncio.run(mgr.broadcast({"type": "games", "data": [{"gameId": "G1"}]}))
    asyncio.run(mgr.broadcast({"type": "status", "message": "no live games"}))
    snap = mgr.snapshot()
    assert snap["games"] == [] and snap["status"] == "no live games"
