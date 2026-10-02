"""
Commentary history, persisted to SQLite so it survives restarts and can be replayed to
dashboards that connect (or reconnect) mid-game.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from booth import config

KEEP_PER_GAME = 100
MAX_AGE_S = 24 * 3600


class HistoryStore:
    """SQLite-backed store. The connection is opened lazily on first use so the process can
    still choose the storage (e.g. `use_memory()` for demo mode) after imports have run."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = str(path or config.get().history_db)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def use_memory(self) -> None:
        """Switch to an in-memory store (nothing is written to disk). Only valid before first use."""
        with self._lock:
            if self._conn is not None:
                raise RuntimeError("history store already in use")
            self._path = ":memory:"

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.execute("""CREATE TABLE IF NOT EXISTS moments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, game_id TEXT, payload TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_moments_game ON moments(game_id, id)")
            conn.commit()
            self._conn = conn
        return self._conn

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

    def recent_for_game(self, game_id: str, n: int = 3) -> list[dict]:
        """The last `n` commentary payloads for one game, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM moments WHERE game_id=? ORDER BY id DESC LIMIT ?",
                (game_id, n)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def recent(self, per_game: int = 25, now: float | None = None) -> list[dict]:
        """Newest `per_game` items of each game from the last 24h, oldest first."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM moments m WHERE ts >= ? AND id IN "
                "(SELECT id FROM moments WHERE game_id=m.game_id ORDER BY id DESC LIMIT ?) "
                "ORDER BY id", (now - MAX_AGE_S, per_game)).fetchall()
        return [json.loads(r[0]) for r in rows]


history = HistoryStore()
