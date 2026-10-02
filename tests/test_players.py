from booth.players import PlayerWatcher
from tests.test_events import game


def player(name, pid, pts=0, reb=0, ast=0, fouls=0, threes=0, played="1"):
    return {"name": name, "personId": pid, "played": played, "statistics": {
        "points": pts, "reboundsTotal": reb, "assists": ast, "foulsPersonal": fouls,
        "threePointersMade": threes, "threePointersAttempted": threes + 2, "steals": 0, "blocks": 0}}


def box(home=(), away=()):
    return {"homeTeam": {"teamTricode": "BOS", "players": list(home)},
            "awayTeam": {"teamTricode": "LAL", "players": list(away)}}


def types(events):
    return [e["type"] for e in events]


def test_first_box_score_is_a_baseline_and_announces_nothing():
    w = PlayerWatcher()
    assert w.detect(game(period=3), box(home=[player("Tatum", 1, pts=32, fouls=4)])) == []
    # ...and those already-reached milestones are never announced later
    assert w.detect(game(period=3), box(home=[player("Tatum", 1, pts=33, fouls=4)])) == []


def test_points_milestone_announced_once():
    w = PlayerWatcher()
    w.detect(game(), box(home=[player("Tatum", 1, pts=18)]))
    ev = w.detect(game(), box(home=[player("Tatum", 1, pts=21)]))
    assert types(ev) == ["player_milestone"] and ev[0]["event"] == "Tatum reaches 20 points"
    assert ev[0]["key_player"] == "Tatum (BOS)" and "21 pts" in ev[0]["key_stat"]
    assert w.detect(game(), box(home=[player("Tatum", 1, pts=24)])) == []


def test_jumping_several_milestones_announces_only_the_biggest():
    w = PlayerWatcher()
    w.detect(game(), box(away=[player("LeBron", 2, pts=15)]))
    ev = w.detect(game(), box(away=[player("LeBron", 2, pts=41)]))
    assert [e["event"] for e in ev] == ["LeBron reaches 40 points"]


def test_double_and_triple_double():
    w = PlayerWatcher()
    w.detect(game(), box(home=[player("Jokic", 3, pts=9, reb=9, ast=9)]))
    ev = w.detect(game(), box(home=[player("Jokic", 3, pts=12, reb=10, ast=9)]))
    assert ev[0]["event"] == "Jokic has a double-double"
    ev = w.detect(game(), box(home=[player("Jokic", 3, pts=14, reb=10, ast=10)]))
    assert ev[0]["event"] == "Jokic has a triple-double"


def test_hot_from_three():
    w = PlayerWatcher()
    w.detect(game(), box(home=[player("Curry", 4, pts=12, threes=3)]))
    ev = w.detect(game(), box(home=[player("Curry", 4, pts=18, threes=5)]))
    assert "hot from deep" in ev[0]["event"] and "5-7 from three" in ev[0]["key_stat"]


def test_foul_trouble_depends_on_the_period():
    w = PlayerWatcher()
    w.detect(game(period=2), box(home=[player("A", 5, fouls=2)]))
    ev = w.detect(game(period=2), box(home=[player("A", 5, fouls=3)]))
    assert types(ev) == ["foul_trouble"] and "3 fouls" in ev[0]["event"]
    # 3 fouls in the 4th quarter is unremarkable; 5 always is; 6 = fouled out
    w2 = PlayerWatcher()
    w2.detect(game(period=4), box(home=[player("B", 6, fouls=2)]))
    assert w2.detect(game(period=4), box(home=[player("B", 6, fouls=3)])) == []
    assert types(w2.detect(game(period=4), box(home=[player("B", 6, fouls=5)]))) == ["foul_trouble"]
    ev = w2.detect(game(period=4), box(home=[player("B", 6, fouls=6)]))
    assert ev[0]["event"] == "B has fouled out"


def test_foul_count_that_jumps_is_announced_once_at_the_highest_level():
    w = PlayerWatcher()
    w.detect(game(period=2), box(home=[player("A", 5, fouls=1)]))
    ev = w.detect(game(period=2), box(home=[player("A", 5, fouls=4)]))
    assert len(ev) == 1 and "4 fouls" in ev[0]["event"]


def test_bench_players_who_have_not_played_are_ignored():
    w = PlayerWatcher()
    w.detect(game(), box(home=[player("DNP", 7, played="0")]))
    assert w.detect(game(), box(home=[player("DNP", 7, pts=30, played="0")])) == []


def test_output_is_capped_per_poll_and_games_are_independent():
    w = PlayerWatcher()
    g1, g2 = game(gid="G1"), game(gid="G2")
    w.detect(g1, box(home=[player(f"P{i}", i) for i in range(5)]))
    w.detect(g2, box(home=[player("X", 99, pts=5)]))
    ev = w.detect(g1, box(home=[player(f"P{i}", i, pts=20 + i) for i in range(5)]))
    assert len(ev) == 2 and [e["event"] for e in ev] == ["P4 reaches 20 points", "P3 reaches 20 points"]
    assert w.detect(g2, box(home=[player("X", 99, pts=21)]))[0]["game_id"] == "G2"
    w.forget("G1")
    assert w.detect(g1, box(home=[player("P0", 0, pts=50)])) == []   # re-baselined
