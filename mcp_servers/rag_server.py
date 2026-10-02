#!/usr/bin/env python3
"""Historical NBA RAG MCP server — ChromaDB + sentence-transformers for semantic search."""
import json
import os
import sys
import threading

from mcp.server.fastmcp import FastMCP

from _common import mock_enabled, offload, serve, unavailable
from booth import config
from rag.filters import build_where, match_players, match_teams

DB_PATH = str(config.get().rag_db)
COLLECTION_NAME = "nba_history"
EMBED_MODEL = "all-MiniLM-L6-v2"

# ChromaDB phones home with anonymous usage telemetry by default; a booth shouldn't do that implicitly.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

mcp = FastMCP("nba-rag")

_collection = None
_embedder = None
_init_lock = threading.Lock()


def _get_store():
    # Tools run in worker threads and startup warmup runs in another: initialise exactly once.
    global _collection, _embedder
    with _init_lock:
        if _collection is None:
            import chromadb
            from sentence_transformers import SentenceTransformer

            client = chromadb.PersistentClient(path=DB_PATH)
            collection = client.get_or_create_collection(COLLECTION_NAME)
            _embedder = SentenceTransformer(EMBED_MODEL)
            _collection = collection  # publish only after everything loaded
    return _collection, _embedder


def _format_results(results: dict) -> list[dict]:
    out = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]
    for doc, meta, dist in zip(docs, metas, distances):
        out.append({"fact": doc, "relevance": round(1 - dist, 3), **meta})
    return out


# ── Metadata-aware retrieval ──────────────────────────────────────────────────

_meta_cache: dict = {"key": None, "metas": []}


def _all_metadatas(collection) -> list[dict]:
    """Every fact's metadata (cheap: the store holds facts, not documents). Refreshed when the
    fact count changes, e.g. after re-seeding."""
    key = (id(collection), collection.count())
    if _meta_cache["key"] != key:
        _meta_cache.update(key=key, metas=collection.get(include=["metadatas"])["metadatas"] or [])
    return _meta_cache["metas"]


def _known(collection, field: str) -> list[str]:
    return sorted({m[field] for m in _all_metadatas(collection) if isinstance(m.get(field), str)})


def _search(query: str, n_results: int, player: str = "", team: str = "", category: str = "",
            min_year: int = 0, max_year: int = 0) -> dict:
    """Semantic search narrowed by whichever metadata filters resolve to stored values.
    A filter that matches nothing in the database is dropped (with a note) rather than returning
    an empty result — the agent still gets the closest semantic matches, and knows why."""
    collection, embedder = _get_store()
    count = collection.count()
    if count == 0:
        raise RuntimeError("database is empty — run 'uv run python rag/seed.py'")

    notes, applied = [], {}
    players = teams = None
    if player:
        players = match_players(_known(collection, "player"), player)
        if players:
            applied["player"] = players
        else:
            notes.append(f"No facts are tagged with player '{player}'; showing semantic matches instead.")
    if team:
        teams = match_teams(_known(collection, "team"), team)
        if teams:
            applied["team"] = teams
        else:
            notes.append(f"No facts are tagged with team '{team}'; showing semantic matches instead.")
    if category:
        if category in _known(collection, "category"):
            applied["category"] = category
        else:
            notes.append(f"Unknown category '{category}'. Valid: {', '.join(_known(collection, 'category'))}.")
            category = ""
    if min_year:
        applied["min_year"] = min_year
    if max_year:
        applied["max_year"] = max_year

    where = build_where(players, teams, category, min_year, max_year)
    kwargs = {"where": where} if where else {}
    results = collection.query(query_embeddings=[embedder.encode(query).tolist()],
                               n_results=min(max(n_results, 1), 10, count), **kwargs)
    facts = _format_results(results)
    if where and not facts:
        notes.append("Filters matched no facts (e.g. year range too narrow).")
    return {"filters_applied": applied, "notes": notes, "facts": facts}


@offload(mcp)
def search_historical_games(query: str, n_results: int = 3, player: str = "", team: str = "",
                            category: str = "", min_year: int = 0, max_year: int = 0) -> str:
    """Semantic search over NBA historical facts, records and milestones. Optional filters narrow
    results to exact metadata: player (e.g. "LeBron"), team (tricode, nickname or name), category
    (see describe_database), min_year/max_year. Use filters when the moment is about a specific
    player or team; use describe_database to see what the store contains."""
    try:
        return json.dumps(_search(query, n_results, player, team, category, min_year, max_year), indent=2)
    except Exception as e:
        if not mock_enabled():
            return unavailable("Historical database", str(e))
        return json.dumps({"note": f"RAG error ({e}), returning mock historical fact", "facts": [{
            "fact": "The last time a rookie averaged 25+ points and 10+ assists through his first "
                    "five games was Magic Johnson in 1979.",
            "relevance": 0.91, "year": 1979, "category": "rookie_record"}]}, indent=2)


@offload(mcp)
def get_player_history(player_name: str) -> str:
    """Historical facts and records tagged with a specific player (name matching is forgiving:
    "LeBron", "LeBron James" and "lebron" all work)."""
    try:
        result = _search(f"{player_name} career records history milestones achievements", 5, player=player_name)
        return json.dumps({"player": player_name, **result}, indent=2)
    except Exception as e:
        if not mock_enabled():
            return unavailable("Historical database", str(e))
        return json.dumps({"note": f"Player history error ({e}), returning mock", "player": player_name,
                           "facts": [{"fact": f"{player_name} is on pace for one of the most efficient "
                                              "scoring seasons in NBA history.", "relevance": 0.88}]}, indent=2)


@offload(mcp)
def search_team_history(team_name: str, context: str = "") -> str:
    """Historical facts about a team/franchise (tricode, nickname or full name), optionally
    steered by `context` (e.g. "comeback in the playoffs")."""
    try:
        query = f"{team_name} {context} history records franchise".strip()
        result = _search(query, 5, team=team_name)
        return json.dumps({"team": team_name, **result}, indent=2)
    except Exception as e:
        if not mock_enabled():
            return unavailable("Historical database", str(e))
        return json.dumps({"note": f"Team history error ({e}), returning mock", "team": team_name,
                           "facts": [{"fact": f"The {team_name} last won back-to-back championships in "
                                              "2010, their 16th franchise title.", "relevance": 0.85}]}, indent=2)


@offload(mcp)
def describe_database() -> str:
    """What the historical database contains: fact count, categories, year range, known players
    and teams. Call this to pick valid filter values for search_historical_games."""
    try:
        collection, _ = _get_store()
        metas = _all_metadatas(collection)
        years = [m["year"] for m in metas if isinstance(m.get("year"), int)]
        cats: dict[str, int] = {}
        for m in metas:
            if "category" in m:
                cats[m["category"]] = cats.get(m["category"], 0) + 1
        return json.dumps({
            "facts": collection.count(), "categories": cats,
            "years": [min(years), max(years)] if years else None,
            "players": _known(collection, "player")[:60], "teams": _known(collection, "team")[:40],
        }, indent=2)
    except Exception as e:
        return unavailable("Historical database", str(e))


def _warmup() -> None:
    """Load Chroma + the embedding model once at startup instead of on the first event."""
    try:
        _get_store()
    except Exception as e:  # tools will report the same error to the agent
        print(f"RAG warmup failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    serve(mcp, warmup=_warmup)
