import hashlib
import json
import uuid

import chromadb
import numpy as np
import pytest

import rag_server
from rag import seed
from rag.facts import (
    LEADER_STATS, leaders_to_facts, load_data_dir, load_jsonl, merge_facts, validate_fact,
)
from rag.filters import build_where, match_players, match_teams


# ── Filters (pure) ────────────────────────────────────────────────────────────

PLAYERS = ["Wilt Chamberlain", "LeBron James", "Kobe Bryant", "Michael Jordan", "Magic Johnson"]
TEAMS = ["Los Angeles Lakers", "Minneapolis Lakers", "Boston Celtics", "Philadelphia Warriors", "multiple"]


def test_player_matching_is_forgiving():
    assert match_players(PLAYERS, "LeBron James") == ["LeBron James"]
    assert match_players(PLAYERS, "lebron") == ["LeBron James"]
    assert match_players(PLAYERS, "Jordan") == ["Michael Jordan"]
    assert match_players(PLAYERS, "Wilt") == ["Wilt Chamberlain"]
    assert match_players(PLAYERS, "Nobody Here") == []


def test_team_matching_by_tricode_nickname_and_franchise_history():
    assert match_teams(TEAMS, "LAL") == ["Los Angeles Lakers", "Minneapolis Lakers"]
    assert match_teams(TEAMS, "Celtics") == ["Boston Celtics"]
    assert match_teams(TEAMS, "BOS") == ["Boston Celtics"]
    assert match_teams(TEAMS, "Knicks") == []


def test_build_where():
    assert build_where() is None
    assert build_where(players=["A"]) == {"player": "A"}
    assert build_where(players=["A", "B"]) == {"player": {"$in": ["A", "B"]}}
    assert build_where(teams=["T"], min_year=1990, max_year=2000) == {
        "$and": [{"team": "T"}, {"year": {"$gte": 1990}}, {"year": {"$lte": 2000}}]}


# ── Ingestion ─────────────────────────────────────────────────────────────────

GOOD = {"id": "x1", "text": "A perfectly reasonable historical sentence.", "player": "A", "year": 1999}


def test_validate_fact_accepts_good_and_strips_none():
    assert validate_fact({**GOOD, "team": None}) == GOOD


@pytest.mark.parametrize("bad", [
    {**GOOD, "id": ""}, {**GOOD, "text": "too short"}, {**GOOD, "year": "1999"},
    {**GOOD, "tags": ["a", "b"]}, "not a dict",
])
def test_validate_fact_rejects_bad_data(bad):
    with pytest.raises(ValueError):
        validate_fact(bad)


def test_duplicate_ids_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        merge_facts([GOOD], [dict(GOOD)])


def test_jsonl_loading_reports_file_and_line(tmp_path):
    f = tmp_path / "facts.jsonl"
    f.write_text("# comment\n" + json.dumps(GOOD) + "\n\n" + '{"id": "y", "text": "short"}\n')
    with pytest.raises(ValueError, match=r"facts\.jsonl:4"):
        load_jsonl(f)
    f.write_text(json.dumps(GOOD) + "\nnot json\n")
    with pytest.raises(ValueError, match=r"facts\.jsonl:2: invalid JSON"):
        load_jsonl(f)
    f.write_text(json.dumps(GOOD) + "\n")
    assert load_data_dir(tmp_path) == [GOOD]
    assert load_data_dir(tmp_path / "missing") == []


def test_leaders_to_facts_from_nba_api_shaped_rows():
    rows = {"PTSLeaders": [
        {"PLAYER_ID": 2544, "PLAYER_NAME": "LeBron James", "PTS": 42184, "PTS_RANK": 1, "IS_ACTIVE_FLAG": "Y"},
        {"PLAYER_ID": 255, "PLAYER_NAME": "Kareem Abdul-Jabbar", "PTS": 38387, "PTS_RANK": 2, "IS_ACTIVE_FLAG": "N"},
        {"PLAYER_ID": 1, "PLAYER_NAME": None, "PTS": 5, "PTS_RANK": 3},          # malformed rows are skipped
    ], "ASTLeaders": []}
    facts = leaders_to_facts(rows)
    assert [f["id"] for f in facts] == ["alltime_pts_1_2544", "alltime_pts_2_255"]
    assert facts[0]["text"] == "LeBron James ranks #1 in NBA history in career points with 42,184 and is still active."
    assert facts[1]["text"].endswith("with 38,387.") and facts[1]["active"] is False
    assert facts[0]["player"] == "LeBron James" and facts[0]["category"] == "career_leader"
    assert set(LEADER_STATS) >= {"PTSLeaders"}


def test_curated_seed_facts_are_valid_and_unique():
    facts = seed.collect_facts()
    assert len(facts) >= 18 and len({f["id"] for f in facts}) == len(facts)


# ── Tool behaviour against a real (in-process) Chroma ─────────────────────────

class FakeEmbedder:
    """Deterministic bag-of-words hashing embedder, so tests need no model download."""
    def encode(self, text):
        v = np.zeros(64)
        for tok in text.lower().replace(".", " ").split():
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % 64] += 1
        return v / (np.linalg.norm(v) or 1)


FACTS = [
    {"id": "a", "text": "LeBron James became the youngest player to score 30000 career points.",
     "player": "LeBron James", "team": "Los Angeles Lakers", "year": 2018, "category": "career_record"},
    {"id": "b", "text": "Kobe Bryant scored 81 points against the Toronto Raptors in a single game.",
     "player": "Kobe Bryant", "team": "Los Angeles Lakers", "year": 2006, "category": "scoring_record"},
    {"id": "c", "text": "George Mikan led the Minneapolis Lakers to five championships in six seasons.",
     "player": "George Mikan", "team": "Minneapolis Lakers", "year": 1950, "category": "championship"},
    {"id": "d", "text": "Larry Bird and the Boston Celtics won the 1986 championship with a 67-15 record.",
     "player": "Larry Bird", "team": "Boston Celtics", "year": 1986, "category": "championship"},
]


@pytest.fixture
def store(monkeypatch):
    embedder = FakeEmbedder()
    col = chromadb.EphemeralClient().create_collection(f"t_{uuid.uuid4().hex}")
    col.add(ids=[f["id"] for f in FACTS], documents=[f["text"] for f in FACTS],
            embeddings=[embedder.encode(f["text"]).tolist() for f in FACTS],
            metadatas=[{k: v for k, v in f.items() if k not in ("id", "text")} for f in FACTS])
    monkeypatch.setattr(rag_server, "_get_store", lambda: (col, embedder))
    rag_server._meta_cache["key"] = None
    return col


def ask(**kw):
    return json.loads(rag_server.search_historical_games(**kw))


def test_player_filter_beats_similar_sounding_facts(store):
    # The query mentions scoring/points/game, which Kobe's fact matches best semantically
    out = ask(query="scored points in a single game", player="LeBron")
    assert [f["player"] for f in out["facts"]] == ["LeBron James"]
    assert out["filters_applied"] == {"player": ["LeBron James"]}


def test_team_filter_by_tricode_covers_franchise_history(store):
    out = ask(query="championships", team="LAL", n_results=5)
    assert {f["team"] for f in out["facts"]} == {"Los Angeles Lakers", "Minneapolis Lakers"}


def test_year_and_category_filters_combine(store):
    out = ask(query="championship", category="championship", min_year=1960)
    assert [f["player"] for f in out["facts"]] == ["Larry Bird"]
    assert ask(query="anything", max_year=1960)["facts"][0]["player"] == "George Mikan"


def test_unknown_filter_values_fall_back_to_semantic_with_a_note(store):
    out = ask(query="points in a game", player="Nobody Atall", category="bogus")
    assert out["facts"] and out["filters_applied"] == {}
    assert any("Nobody Atall" in n for n in out["notes"]) and any("bogus" in n for n in out["notes"])


def test_filters_matching_nothing_say_so(store):
    out = ask(query="anything", player="Kobe", min_year=2020)
    assert out["facts"] == [] and any("no facts" in n.lower() for n in out["notes"])


def test_player_and_team_history_tools(store):
    p = json.loads(rag_server.get_player_history("kobe"))
    assert [f["player"] for f in p["facts"]] == ["Kobe Bryant"]
    t = json.loads(rag_server.search_team_history("Celtics", "championship"))
    assert [f["team"] for f in t["facts"]] == ["Boston Celtics"]


def test_describe_database(store):
    d = json.loads(rag_server.describe_database())
    assert d["facts"] == 4 and d["years"] == [1950, 2018]
    assert d["categories"] == {"career_record": 1, "scoring_record": 1, "championship": 2}
    assert "LeBron James" in d["players"] and "Boston Celtics" in d["teams"]


def test_empty_database_is_reported_unavailable(monkeypatch):
    col = chromadb.EphemeralClient().create_collection(f"e_{uuid.uuid4().hex}")
    monkeypatch.setattr(rag_server, "_get_store", lambda: (col, FakeEmbedder()))
    monkeypatch.delenv("BOOTH_MOCK_DATA", raising=False)
    assert "unavailable" in json.loads(rag_server.search_historical_games("x"))["error"]
