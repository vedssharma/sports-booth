from booth.events import (
    CLOSE_GAME_COOLDOWN_S, RUN_WINDOW_S, UPDATE_INTERVAL_S, EventDetector,
)


def game(home=0, away=0, period=1, status="Q1", gid="G1", game_status=2):
    return {
        "gameId": gid, "period": period, "gameClock": "5:00", "gameStatusText": status,
        "gameStatus": game_status,
        "homeTeam": {"teamTricode": "BOS", "score": home},
        "awayTeam": {"teamTricode": "LAL", "score": away},
    }


def types(events):
    return [e["type"] for e in events]


def test_first_sight_of_live_game_emits_join_event():
    d = EventDetector()
    assert types(d.detect([game(10, 8)], now=0)) == ["game_update"]


def test_first_sight_of_final_game_is_ignored():
    d = EventDetector()
    assert d.detect([game(100, 90, 4, "Final", game_status=3)], now=0) == []


def test_period_change_emits_quarter_start():
    d = EventDetector()
    d.detect([game(20, 20, 1)], now=0)
    ev = d.detect([game(30, 28, 2)], now=45)
    assert types(ev) == ["quarter_start"]


def test_scoring_run_detected_across_multiple_polls():
    d = EventDetector()
    d.detect([game(20, 20)], now=0)
    # +4 unanswered per poll: no single poll reaches 7, but the window does
    assert d.detect([game(24, 20)], now=45) == []
    ev = d.detect([game(28, 20)], now=90)
    assert types(ev) == ["scoring_run"]
    assert ev[0]["event"] == "BOS on a 8-0 run"


def test_run_outside_window_is_not_flagged_and_run_does_not_refire():
    d = EventDetector()
    d.detect([game(20, 20)], now=0)
    assert types(d.detect([game(28, 20)], now=45)) == ["scoring_run"]
    # Same score, immediately after: window was reset, no repeat
    assert all(t != "scoring_run" for t in types(d.detect([game(29, 20)], now=90)))
    # Slow trickle spread beyond the window never trips the threshold
    d2 = EventDetector()
    d2.detect([game(0, 0)], now=0)
    t = 0
    for pts in range(2, 12, 2):
        t += RUN_WINDOW_S + 1
        assert "scoring_run" not in types(d2.detect([game(pts, 0)], now=t))


def test_away_run_reports_away_team():
    d = EventDetector()
    d.detect([game(20, 20)], now=0)
    ev = d.detect([game(20, 29)], now=45)
    assert ev[0]["event"] == "LAL on a 9-0 run"


def test_crunch_time_is_rate_limited():
    d = EventDetector()
    d.detect([game(90, 88, 4)], now=0)
    assert types(d.detect([game(92, 88, 4)], now=45)) == ["close_game"]
    assert d.detect([game(92, 90, 4)], now=90) == []
    assert types(d.detect([game(94, 90, 4)], now=45 + CLOSE_GAME_COOLDOWN_S)) == ["close_game"]


def test_routine_update_only_after_quiet_stretch():
    d = EventDetector()
    d.detect([game(10, 10)], now=0)
    assert d.detect([game(10, 10)], now=45) == []
    assert types(d.detect([game(12, 10)], now=UPDATE_INTERVAL_S)) == ["game_update"]


def test_final_event_emitted_exactly_once():
    d = EventDetector()
    d.detect([game(100, 98, 4)], now=0)
    final = game(104, 98, 4, "Final", game_status=3)
    ev = d.detect([final], now=45)
    assert types(ev) == ["game_final"]
    assert "BOS win by 6" in ev[0]["event"]
    assert d.detect([final], now=90) == []


def test_games_tracked_independently():
    d = EventDetector()
    d.detect([game(0, 0, gid="A"), game(0, 0, gid="B")], now=0)
    ev = d.detect([game(10, 0, gid="A"), game(2, 0, gid="B")], now=45)
    assert [(e["game_id"], e["type"]) for e in ev] == [("A", "scoring_run")]


# ── Lead changes & comebacks ──────────────────────────────────────────────────

def test_lead_change_when_lead_flips():
    d = EventDetector()
    d.detect([game(40, 36, 2)], now=0)        # BOS leads
    ev = d.detect([game(40, 41, 2)], now=45)  # LAL takes it (net swing 5 < run threshold)
    assert types(ev) == ["lead_change"]
    assert ev[0]["event"] == "LAL takes the lead" and not ev[0].get("comeback")


def test_going_through_a_tie_still_counts_as_a_flip_but_a_tie_alone_does_not():
    d = EventDetector()
    d.detect([game(40, 36, 2)], now=0)
    assert "lead_change" not in types(d.detect([game(41, 41, 2)], now=45))   # tied: nobody took over
    assert types(d.detect([game(41, 43, 2)], now=90)) == ["lead_change"]     # LAL now leads


def test_no_lead_change_events_in_the_opening_minutes():
    d = EventDetector()
    d.detect([game(8, 6, 1)], now=0)
    assert d.detect([game(8, 10, 1)], now=45) == []


def test_lead_change_is_rate_limited_in_a_seesaw():
    d = EventDetector()
    d.detect([game(40, 36, 2)], now=0)
    assert types(d.detect([game(40, 41, 2)], now=45)) == ["lead_change"]
    assert "lead_change" not in types(d.detect([game(44, 41, 2)], now=90))   # flips back within cooldown


def test_comeback_is_called_out_with_the_size_of_the_hole():
    d = EventDetector()
    d.detect([game(40, 52, 2)], now=0)          # BOS down 12
    d.detect([game(44, 53, 2)], now=200)
    ev = d.detect([game(54, 53, 2)], now=400)   # BOS takes the lead (outside the run window)
    assert types(ev) == ["lead_change"] and ev[0]["comeback"] and ev[0]["deficit_overcome"] == 12
    assert "comeback from 12 down" in ev[0]["event"]


def test_a_flip_caused_by_a_big_run_is_reported_as_the_run():
    d = EventDetector()
    d.detect([game(40, 52, 2)], now=0)
    assert types(d.detect([game(52, 51, 2)], now=45)) == ["scoring_run"]


def test_old_hole_does_not_count_as_a_comeback_after_leading_for_a_while():
    d = EventDetector()
    d.detect([game(10, 22, 1)], now=0)          # BOS was down 12 early...
    d.detect([game(60, 50, 2)], now=200)        # ...then led comfortably (hole resets)
    d.detect([game(60, 62, 3)], now=400)        # LAL flips
    ev = d.detect([game(70, 62, 3)], now=800)   # BOS flips back later
    assert types(ev) == ["lead_change"] and not ev[0].get("comeback")
