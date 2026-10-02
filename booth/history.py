"""
Commentary history, persisted to SQLite so it survives restarts and can be replayed to
dashboards that connect (or reconnect) mid-game.
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEFAULT_PATH = ROOT / "data" / "history.db"
KEEP_PER_GAME = 100
MAX_AGE_S = 24 * 3600


class HistoryStore:
    def __init__(self, path: str | Path | None = None) -> None:
        path = str(path or os.getenv("BOOTH_HISTORY_DB") or DEFAULT_PATH)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS moments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, game_id TEXT, payload TEXT)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_moments_game ON moments(game_id, id)")
        self._db.commit()

    def add(self, commentary: dict, now: float | None = None) -> None:
        """Store one commentary payload ({event, analyst, historian, degenerate})."""
        now = time.time() if now is None else now
        game_id = (commentary.get("event") or {}).get("game_id", "")
        with self._lock, self._db:
            self._db.execute("INSERT INTO moments (ts, game_id, payload) VALUES (?,?,?)",
                             (now, game_id, json.dumps(commentary)))
            self._db.execute(
                "DELETE FROM moments WHERE game_id=? AND id NOT IN "
                "(SELECT id FROM moments WHERE game_id=? ORDER BY id DESC LIMIT ?)",
                (game_id, game_id, KEEP_PER_GAME))
            self._db.execute("DELETE FROM moments WHERE ts < ?", (now - MAX_AGE_S,))

    def recent(self, per_game: int = 25, now: float | None = None) -> list[dict]:
        """Newest `per_game` items of each game from the last 24h, oldest first."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM moments m WHERE ts >= ? AND id IN "
                "(SELECT id FROM moments WHERE game_id=m.game_id ORDER BY id DESC LIMIT ?) "
                "ORDER BY id", (now - MAX_AGE_S, per_game)).fetchall()
        return [json.loads(r[0]) for r in rows]


history = HistoryStore(":memory:" if os.getenv("BOOTH_MOCK_DATA") == "1" else None)
