#!/usr/bin/env python3
"""Historical NBA RAG MCP server — ChromaDB + sentence-transformers for semantic search."""
import json
import sys
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from _common import mock_enabled, offload, serve, unavailable

DB_PATH = str(Path(__file__).parent.parent / "rag" / "chroma_db")
COLLECTION_NAME = "nba_history"
EMBED_MODEL = "all-MiniLM-L6-v2"

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


@offload(mcp)
def search_historical_games(query: str, n_results: int = 3) -> str:
    """Semantic search over NBA historical game facts, records, and milestone moments."""
    try:
        collection, embedder = _get_store()
        count = collection.count()
        if count == 0:
            if not mock_enabled():
                return unavailable("Historical database", "empty — run 'uv run python rag/seed.py'")
            return (
                "Historical database is empty. "
                "Run 'uv run python rag/seed.py' to populate it.\n"
                "Mock fact: This is the first time a player has scored 30+ in 3 consecutive "
                "playoff games as a rookie since Kareem Abdul-Jabbar in 1970."
            )
        embedding = embedder.encode(query).tolist()
        results = collection.query(
            query_embeddings=[embedding],
            n_results=min(n_results, count),
        )
        facts = _format_results(results)
        return json.dumps(facts, indent=2)
    except Exception as e:
        if not mock_enabled():
            return unavailable("Historical database", str(e))
        return json.dumps({
            "note": f"RAG error ({e}), returning mock historical fact",
            "facts": [
                {
                    "fact": "The last time a rookie averaged 25+ points and 10+ assists "
                            "through his first five games was Magic Johnson in 1979.",
                    "relevance": 0.91,
                    "year": 1979,
                    "category": "rookie_record",
                }
            ],
        }, indent=2)


@offload(mcp)
def get_player_history(player_name: str) -> str:
    """Retrieve historical facts and records specifically about a player."""
    try:
        collection, embedder = _get_store()
        count = collection.count()
        if count == 0:
            return unavailable("Historical database", "empty — run 'uv run python rag/seed.py'")
        query = f"{player_name} career records history milestones achievements"
        embedding = embedder.encode(query).tolist()
        results = collection.query(
            query_embeddings=[embedding],
            n_results=min(3, count),
        )
        facts = _format_results(results)
        return json.dumps({"player": player_name, "historical_facts": facts}, indent=2)
    except Exception as e:
        if not mock_enabled():
            return unavailable("Historical database", str(e))
        return json.dumps({
            "note": f"Player history error ({e}), returning mock",
            "player": player_name,
            "historical_facts": [
                {
                    "fact": f"{player_name} is on pace for one of the most efficient "
                            "scoring seasons in NBA history through the first 10 games.",
                    "relevance": 0.88,
                }
            ],
        }, indent=2)


@offload(mcp)
def search_team_history(team_name: str, context: str = "") -> str:
    """Search for historical facts about a team, optionally filtered by context."""
    try:
        collection, embedder = _get_store()
        count = collection.count()
        if count == 0:
            return unavailable("Historical database", "empty — run 'uv run python rag/seed.py'")
        query = f"{team_name} {context} history records franchise".strip()
        embedding = embedder.encode(query).tolist()
        results = collection.query(
            query_embeddings=[embedding],
            n_results=min(3, count),
        )
        facts = _format_results(results)
        return json.dumps({"team": team_name, "historical_facts": facts}, indent=2)
    except Exception as e:
        if not mock_enabled():
            return unavailable("Historical database", str(e))
        return json.dumps({
            "note": f"Team history error ({e}), returning mock",
            "team": team_name,
            "historical_facts": [
                {
                    "fact": f"The {team_name} last won back-to-back championships in 2010, "
                            "their 16th franchise title.",
                    "relevance": 0.85,
                }
            ],
        }, indent=2)


def _warmup() -> None:
    """Load Chroma + the embedding model once at startup instead of on the first event."""
    try:
        _get_store()
    except Exception as e:  # tools will report the same error to the agent
        print(f"RAG warmup failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    serve(mcp, warmup=_warmup)
