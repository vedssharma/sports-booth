"""
Player-level moments from live box scores: scoring/three-point milestones, double- and
triple-doubles, and foul trouble.

The scoreboard has no player data, so this needs one box score per live game per poll. The first
box score seen for a game is treated as a baseline: milestones already reached are remembered
but not announced (otherwise joining a game in Q3 would flood the booth with old news).
"""
from booth.events import game_label, game_score

POINT_MILESTONES = (20, 30, 40, 50, 60)
THREE_MILESTONES = (5, 7, 9)
MAX_EVENTS_PER_GAME_PER_POLL = 2   # a quiet booth beats a noisy one

# Foul trouble: (min fouls, last period it applies in). 5+ always counts; 6 = fouled out.
FOUL_RULES = ((3, 2), (4, 3), (5, 99), (6, 99))


def _ordinal_stats(stats: dict) -> dict:
    return {
        "pts": stats.get("points", 0), "reb": stats.get("reboundsTotal", 0),
        "ast": stats.get("assists", 0), "stl": stats.get("steals", 0), "blk": stats.get("blocks", 0),
    }


def _double_digit_count(s: dict) -> int:
    return sum(1 for v in s.values() if v >= 10)


def _line(s: dict) -> str:
    return f"{s['pts']} pts, {s['reb']} reb, {s['ast']} ast"


def _achievements(player: dict, period: int) -> list[tuple[str, int, str]]:
    """Everything this player has reached so far as (key, rank, kind). Higher rank = bigger news."""
    pid = player.get("personId") or player.get("name")
    raw = player.get("statistics", {})
    s = _ordinal_stats(raw)
    out: list[tuple[str, int, str]] = []
    for t in POINT_MILESTONES:
        if s["pts"] >= t:
            out.append((f"{pid}:pts:{t}", t, "points"))
    for t in THREE_MILESTONES:
        if raw.get("threePointersMade", 0) >= t:
            out.append((f"{pid}:3pm:{t}", 30 + t, "threes"))
    dd = _double_digit_count(s)
    if dd >= 2:
        out.append((f"{pid}:dd", 80, "double_double"))
    if dd >= 3:
        out.append((f"{pid}:td", 100, "triple_double"))
    fouls = raw.get("foulsPersonal", 0)
    for n, last_period in FOUL_RULES:
        if fouls >= n and period <= last_period:
            out.append((f"{pid}:fouls:{n}", 10 + n, "fouls"))
        elif fouls >= n:
            # Past the window where this count mattered — mark as seen, never announce
            out.append((f"{pid}:fouls:{n}", -1, "fouls"))
    return out


class PlayerWatcher:
    def __init__(self) -> None:
        self._seen: dict[str, set[str]] = {}

    def forget(self, game_id: str) -> None:
        self._seen.pop(game_id, None)

    def detect(self, game: dict, box: dict) -> list[dict]:
        """`game` = scoreboard entry (period, clock, score); `box` = live boxscore `game` dict."""
        gid = game.get("gameId", "")
        period = game.get("period", 0) or 0
        baseline = gid not in self._seen
        seen = self._seen.setdefault(gid, set())

        fresh: list[tuple[int, str, str, dict, str]] = []   # (rank, key, kind, player, team)
        for side in ("homeTeam", "awayTeam"):
            team = box.get(side, {})
            for player in team.get("players", []):
                if player.get("played") != "1":
                    continue
                for key, rank, kind in _achievements(player, period):
                    if key in seen:
                        continue
                    seen.add(key)
                    if not baseline and rank >= 0:
                        fresh.append((rank, key, kind, player, team.get("teamTricode", "")))
        if baseline:
            return []

        # A player who jumps several milestones in one poll only gets the biggest announcement
        best: dict[str, tuple[int, str, str, dict, str]] = {}
        for item in fresh:
            name = item[3].get("name", "")
            if item[2] == "fouls":
                best[f"{name}:fouls"] = max(best.get(f"{name}:fouls", item), item, key=lambda i: i[0])
            else:
                best[name] = max(best.get(name, item), item, key=lambda i: i[0])
        # biggest news first; on equal rank, the player with more points
        chosen = sorted(best.values(), key=lambda i: (i[0], i[3].get("statistics", {}).get("points", 0)),
                        reverse=True)
        foul_events = [i for i in chosen if i[2] == "fouls"]
        other = [i for i in chosen if i[2] != "fouls"][:MAX_EVENTS_PER_GAME_PER_POLL]
        return [self._event(game, i) for i in other + foul_events[:MAX_EVENTS_PER_GAME_PER_POLL]]

    def _event(self, game: dict, item: tuple) -> dict:
        _, key, kind, player, team = item
        name = player.get("name", "A player")
        raw = player.get("statistics", {})
        s = _ordinal_stats(raw)
        fouls = raw.get("foulsPersonal", 0)
        threshold = int(key.rsplit(":", 1)[-1]) if kind in ("points", "threes") else 0
        if kind == "triple_double":
            text, etype = f"{name} has a triple-double", "player_milestone"
        elif kind == "double_double":
            text, etype = f"{name} has a double-double", "player_milestone"
        elif kind == "points":
            text, etype = f"{name} reaches {threshold} points", "player_milestone"
        elif kind == "threes":
            text, etype = f"{name} is hot from deep — {raw.get('threePointersMade', 0)} threes", "player_milestone"
        else:
            etype = "foul_trouble"
            text = f"{name} has fouled out" if fouls >= 6 else f"{name} in foul trouble — {fouls} fouls"
        stat = (f"{raw.get('threePointersMade', 0)}-{raw.get('threePointersAttempted', 0)} from three, "
                if kind == "threes" else "") + _line(s) + f", {fouls} PF"
        return {
            "type": etype, "game": game_label(game), "game_id": game.get("gameId", ""),
            "quarter": game.get("period", 0), "time_remaining": game.get("gameClock", ""),
            "score": game_score(game), "event": text, "key_player": f"{name} ({team})",
            "key_stat": stat, "context": f"{name} ({team}) now: {stat}.",
        }
