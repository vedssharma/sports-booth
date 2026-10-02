"""
The Odds API client + SQLite snapshot store.

Shared by the betting MCP server (tools) and main.py (records a snapshot the first time a game is
seen, so "opening" lines and line movement are real). Snapshots live in data/odds.db.
"""
import os
import sqlite3
import statistics
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT = "basketball_nba"
DB_PATH = Path(os.getenv("BOOTH_ODDS_DB", ROOT / "data" / "odds.db"))

TEAM_NAMES = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}


# ── Pure helpers ──────────────────────────────────────────────────────────────

def resolve_teams(text: str) -> set[str]:
    """Find NBA teams (as Odds-API full names) mentioned in free text via tricode or nickname."""
    found = set()
    upper_tokens = set(text.upper().replace("@", " ").replace("-", " ").split())
    lower = text.lower()
    for code, name in TEAM_NAMES.items():
        nickname = name.split()[-1].lower()
        if code in upper_tokens or name.lower() in lower or nickname in lower.split():
            found.add(name)
    return found


def american_to_prob(ml: float) -> float:
    """Implied win probability from an American moneyline (vig not removed)."""
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def normalize_event(ev: dict) -> dict:
    """Collapse all bookmakers into one consensus (median) line for the home team."""
    home, away = ev.get("home_team"), ev.get("away_team")
    spreads, totals, ml_home, ml_away = [], [], [], []
    for bm in ev.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            outs = {o["name"]: o for o in mkt.get("outcomes", [])}
            if mkt["key"] == "spreads" and home in outs and outs[home].get("point") is not None:
                spreads.append(outs[home]["point"])
            elif mkt["key"] == "totals" and "Over" in outs and outs["Over"].get("point") is not None:
                totals.append(outs["Over"]["point"])
            elif mkt["key"] == "h2h":
                if home in outs:
                    ml_home.append(outs[home]["price"])
                if away in outs:
                    ml_away.append(outs[away]["price"])
    return {
        "event_id": ev.get("id"), "home": home, "away": away,
        "commence_time": ev.get("commence_time"),
        "spread_home": _median(spreads), "total": _median(totals),
        "ml_home": _median(ml_home), "ml_away": _median(ml_away),
        "books": len(ev.get("bookmakers", [])),
    }


def key_number_note(spread_home: float | None) -> str | None:
    """NBA key numbers are far less pronounced than NFL, but 3/6/7/10 still matter a bit."""
    if spread_home is None:
        return None
    s = abs(spread_home)
    near = min((3, 6, 7, 10), key=lambda k: abs(k - s))
    gap = round(s - near, 1)
    if gap == 0:
        return f"Spread sits exactly on {near}."
    return f"Spread is {abs(gap)} {'above' if gap > 0 else 'below'} the {near} key number."


# ── Network ───────────────────────────────────────────────────────────────────

def fetch_events(api_key: str) -> list[dict]:
    with httpx.Client(timeout=10) as client:
        resp = client.get(
            f"{ODDS_API_BASE}/sports/{SPORT}/odds/",
            params={"apiKey": api_key, "regions": "us", "markets": "spreads,totals,h2h",
                    "oddsFormat": "american"},
        )
        resp.raise_for_status()
        return [normalize_event(e) for e in resp.json()]


# ── Snapshot store ────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        ts REAL, event_id TEXT, home TEXT, away TEXT,
        spread_home REAL, total REAL, ml_home REAL, ml_away REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(event_id, ts)")
    return conn


def record_snapshots(events: list[dict], now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
            [(now, e["event_id"], e["home"], e["away"], e["spread_home"], e["total"],
              e["ml_home"], e["ml_away"]) for e in events],
        )


SNAPSHOT_TTL_S = float(os.getenv("BOOTH_ODDS_TTL", "60"))
_last: dict = {"at": float("-inf"), "events": []}
_last_lock = threading.Lock()


def snapshot_now(api_key: str) -> list[dict]:
    """Fetch current odds and record them. Returns the normalized events.

    Rate-limited per process: the Odds API free tier is ~500 requests/month and every betting
    tool call used to spend one. Within SNAPSHOT_TTL_S the previous result is reused (and not
    re-recorded as a new snapshot).
    """
    with _last_lock:
        if time.monotonic() - _last["at"] < SNAPSHOT_TTL_S:
            return _last["events"]
        events = fetch_events(api_key)
        record_snapshots(events)
        _last.update(at=time.monotonic(), events=events)
        return events


def movement(event_id: str) -> dict | None:
    """Earliest vs latest snapshot we hold for an event (None if we've only seen it once)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, spread_home, total, ml_home, ml_away FROM snapshots "
            "WHERE event_id=? ORDER BY ts", (event_id,)).fetchall()
    if len(rows) < 2:
        return None
    (t0, s0, tot0, mh0, ma0), (t1, s1, tot1, mh1, ma1) = rows[0], rows[-1]
    delta = lambda a, b: None if a is None or b is None else round(b - a, 1)  # noqa: E731
    return {
        "snapshots": len(rows),
        "tracked_minutes": round((t1 - t0) / 60),
        "opening": {"spread_home": s0, "total": tot0, "ml_home": mh0, "ml_away": ma0},
        "current": {"spread_home": s1, "total": tot1, "ml_home": mh1, "ml_away": ma1},
        "spread_move": delta(s0, s1),
        "total_move": delta(tot0, tot1),
    }
