"""
Live event detection: turns successive NBA scoreboard snapshots into discrete "moments".

Stateful (EventDetector) so it can:
  - detect scoring runs over a rolling window rather than a single poll interval,
  - rate-limit repeats (crunch time, routine updates) with per-game cooldowns,
  - announce lead changes (and comebacks) without flooding when a game is a seesaw,
  - announce a game's final score exactly once.

Player-level moments (milestones, foul trouble) need box scores; see booth/players.py.
"""
import time
from collections import deque
from dataclasses import dataclass, field

# Net points one team must outscore the other by, within RUN_WINDOW_S, to flag a run
SCORING_RUN_THRESHOLD = 7
RUN_WINDOW_S = 180
# Minimum seconds between repeated crunch-time events for one game
CLOSE_GAME_COOLDOWN_S = 120
# Emit a routine "live update" only if nothing else was emitted for this long
UPDATE_INTERVAL_S = 150
# Lead changes: ignore the opening minutes (meaningless flips), and rate-limit repeats
LEAD_CHANGE_MIN_TOTAL_POINTS = 40
LEAD_CHANGE_COOLDOWN_S = 120
# Taking the lead after trailing by at least this many points is called out as a comeback
COMEBACK_DEFICIT = 10


def game_label(game: dict) -> str:
    home = game.get("homeTeam", {}).get("teamTricode", "?")
    away = game.get("awayTeam", {}).get("teamTricode", "?")
    return f"{away} @ {home}"


def game_score(game: dict) -> dict:
    home, away = game.get("homeTeam", {}), game.get("awayTeam", {})
    return {
        home.get("teamTricode", "HOME"): home.get("score", 0) or 0,
        away.get("teamTricode", "AWAY"): away.get("score", 0) or 0,
    }


def is_final(game: dict) -> bool:
    return game.get("gameStatus") == 3 or "final" in game.get("gameStatusText", "").lower()


@dataclass
class _GameState:
    # (timestamp, home_score, away_score) within the current period
    history: deque = field(default_factory=lambda: deque(maxlen=20))
    period: int = 0
    last_event_at: float = 0.0
    last_close_at: float = float("-inf")
    last_lead_change_at: float = float("-inf")
    leader: int = 0                                 # last non-tied leader: +1 home, -1 away
    max_deficit: dict = field(default_factory=lambda: {1: 0, -1: 0})  # biggest hole each side has been in


class EventDetector:
    def __init__(self) -> None:
        self._state: dict[str, _GameState] = {}
        self._finalized: set[str] = set()

    def detect(self, games: list[dict], now: float | None = None) -> list[dict]:
        """
        `games` = every game that has started (live AND final). Returns new events.
        A live game seen for the first time yields a 'game_update' (joining the broadcast);
        a final game seen for the first time is ignored (we never covered it).
        """
        now = time.monotonic() if now is None else now
        events: list[dict] = []
        for game in games:
            gid = game.get("gameId", "")
            if gid in self._finalized:
                continue
            ev = self._detect_game(gid, game, now)
            if ev:
                events.append(ev)
        return events

    def _detect_game(self, gid: str, game: dict, now: float) -> dict | None:
        label = game_label(game)
        score = game_score(game)
        period = game.get("period", 0) or 0
        clock = game.get("gameClock", "")
        status = game.get("gameStatusText", "")
        home, away = game.get("homeTeam", {}), game.get("awayTeam", {})
        hs, as_ = home.get("score", 0) or 0, away.get("score", 0) or 0
        hc, ac = home.get("teamTricode", "HOME"), away.get("teamTricode", "AWAY")

        def make(etype: str, text: str, context: str, **extra) -> dict:
            st.last_event_at = now
            return {"type": etype, "game": label, "game_id": gid, "quarter": period,
                    "time_remaining": clock, "score": score, "event": text, "context": context,
                    **extra}

        st = self._state.get(gid)

        if is_final(game):
            self._finalized.add(gid)
            if st is None:
                return None
            self._state.pop(gid, None)
            winner, loser = (hc, ac) if hs > as_ else (ac, hc)
            result = ("tied" if hs == as_ else f"{winner} win by {abs(hs - as_)}")
            return make("game_final", f"FINAL — {label}: {result}",
                        f"Final score: {ac} {as_} — {hc} {hs}. Went {period} periods.")

        # ── First time we see this live game ──────────────────────────────────
        if st is None:
            st = self._state[gid] = _GameState(period=period)
            st.history.append((now, hs, as_))
            st.leader = (hs > as_) - (hs < as_)
            st.max_deficit[1], st.max_deficit[-1] = max(0, as_ - hs), max(0, hs - as_)
            return make("game_update", f"{label} is live — {status}",
                        f"Joining the broadcast in Q{period}. Score: {ac} {as_} — {hc} {hs}",
                        join=True)

        prev_ts, prev_hs, prev_as = st.history[-1]
        scored = (hs - prev_hs) + (as_ - prev_as) > 0

        # Track who leads and how deep each side's worst hole has been (for lead changes/comebacks)
        st.max_deficit[1] = max(st.max_deficit[1], as_ - hs)
        st.max_deficit[-1] = max(st.max_deficit[-1], hs - as_)
        lead_now = (hs > as_) - (hs < as_)
        flipped_to = 0
        if lead_now and st.leader and lead_now != st.leader:
            flipped_to = lead_now
        hole = st.max_deficit[flipped_to] if flipped_to else 0   # hole the new leader just climbed out of
        if lead_now:
            st.leader = lead_now
            st.max_deficit[lead_now] = 0   # leading again: an old hole no longer counts

        # ── Period change ─────────────────────────────────────────────────────
        if period > st.period:
            prev_period = st.period
            st.period = period
            st.history.clear()
            st.history.append((now, hs, as_))
            q = "Overtime!" if period > 4 else f"Q{period} underway"
            return make("quarter_start", f"{label} — {q}",
                        f"End of Q{prev_period} score: {ac} {prev_as} — {hc} {prev_hs}. "
                        f"Margin: {abs(prev_hs - prev_as)} pts.")

        st.history.append((now, hs, as_))

        # ── Scoring run over the rolling window ───────────────────────────────
        base = next((h for h in st.history if h[0] >= now - RUN_WINDOW_S), st.history[0])
        swing = (hs - base[1]) - (as_ - base[2])
        if abs(swing) >= SCORING_RUN_THRESHOLD:
            run_team, other = (hc, ac) if swing > 0 else (ac, hc)
            run_pts = (hs - base[1]) if swing > 0 else (as_ - base[2])
            opp_pts = (as_ - base[2]) if swing > 0 else (hs - base[1])
            lead = (hs - as_) if swing > 0 else (as_ - hs)
            st.history.clear()  # restart the window so the same run doesn't re-fire
            st.history.append((now, hs, as_))
            return make("scoring_run", f"{run_team} on a {run_pts}-{opp_pts} run",
                        f"{run_team} {'leads' if lead > 0 else 'trails'} by {abs(lead)}. "
                        f"Current score: {ac} {as_} — {hc} {hs}")

        # ── Lead change (a real flip of the lead; ties in between don't count) ─
        if (flipped_to and hs + as_ >= LEAD_CHANGE_MIN_TOTAL_POINTS
                and now - st.last_lead_change_at >= LEAD_CHANGE_COOLDOWN_S):
            st.last_lead_change_at = now
            new_leader, old_leader = (hc, ac) if flipped_to == 1 else (ac, hc)
            if hole >= COMEBACK_DEFICIT:
                return make("lead_change", f"{new_leader} completes a comeback from {hole} down",
                            f"{new_leader} now leads {old_leader} by {abs(hs - as_)}. "
                            f"Score: {ac} {as_} — {hc} {hs}", comeback=True, deficit_overcome=hole)
            return make("lead_change", f"{new_leader} takes the lead",
                        f"{new_leader} up {abs(hs - as_)} over {old_leader}. Score: {ac} {as_} — {hc} {hs}")

        # ── Crunch time (Q4/OT, within 5), rate limited ───────────────────────
        margin = abs(hs - as_)
        if (period >= 4 and margin <= 5 and scored
                and now - st.last_close_at >= CLOSE_GAME_COOLDOWN_S):
            st.last_close_at = now
            who = "Tie game!" if margin == 0 else f"{hc if hs > as_ else ac} leads by {margin}"
            return make("close_game", f"Crunch time — game within {margin}",
                        f"{ac} {as_} — {hc} {hs}. {who}")

        # ── Routine update only after a quiet stretch ─────────────────────────
        if now - st.last_event_at >= UPDATE_INTERVAL_S:
            return make("game_update", f"{label} — live update",
                        f"Current score: {ac} {as_} — {hc} {hs}. Q{period} | {status}")
        return None
