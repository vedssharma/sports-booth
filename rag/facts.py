"""
Historical-fact ingestion helpers (pure Python — no embedding model or database needed).

Facts are plain dicts: {"id", "text", plus optional metadata: player, team, year, category, ...}.
Sources:
  - the curated list in rag/seed.py
  - any *.jsonl file dropped in rag/data/ (one fact per line)
  - all-time career leaderboards pulled from nba_api (rag/seed.py --fetch-leaders)
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
# Metadata values Chroma accepts
_SCALARS = (str, int, float, bool)


def validate_fact(fact: dict, source: str = "fact") -> dict:
    """Return a cleaned copy of `fact` or raise ValueError describing the problem."""
    if not isinstance(fact, dict):
        raise ValueError(f"{source}: expected an object, got {type(fact).__name__}")
    fid, text = fact.get("id"), fact.get("text")
    if not isinstance(fid, str) or not fid.strip():
        raise ValueError(f"{source}: 'id' must be a non-empty string")
    if not isinstance(text, str) or len(text.strip()) < 20:
        raise ValueError(f"{source} ({fid}): 'text' must be a sentence of at least 20 characters")
    clean = {"id": fid.strip(), "text": text.strip()}
    for key, value in fact.items():
        if key in ("id", "text") or value is None:
            continue   # Chroma metadata can't hold None
        if not isinstance(value, _SCALARS):
            raise ValueError(f"{source} ({fid}): metadata '{key}' must be str/int/float/bool")
        clean[key] = value
    if "year" in clean and not isinstance(clean["year"], int):
        raise ValueError(f"{source} ({fid}): 'year' must be an integer")
    return clean


def merge_facts(*groups: list[dict]) -> list[dict]:
    """Validate and combine fact lists. Duplicate ids are an error (a typo would silently
    shadow another fact)."""
    seen: dict[str, str] = {}
    out = []
    for group in groups:
        for fact in group:
            if fact["id"] in seen:
                raise ValueError(f"duplicate fact id '{fact['id']}'")
            seen[fact["id"]] = fact["id"]
            out.append(fact)
    return out


def load_jsonl(path: Path) -> list[dict]:
    facts = []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path.name}:{n}: invalid JSON ({e.msg})") from e
        facts.append(validate_fact(raw, f"{path.name}:{n}"))
    return facts


def load_data_dir(directory: Path = DATA_DIR) -> list[dict]:
    facts: list[dict] = []
    for path in sorted(directory.glob("*.jsonl")) if directory.exists() else []:
        facts.extend(load_jsonl(path))
    return facts


# ── All-time leaderboards (nba_api AllTimeLeadersGrids) ───────────────────────

# result-set name -> (stat column, human label)
LEADER_STATS = {
    "PTSLeaders": ("PTS", "points"),
    "REBLeaders": ("REB", "rebounds"),
    "ASTLeaders": ("AST", "assists"),
    "STLLeaders": ("STL", "steals"),
    "BLKLeaders": ("BLK", "blocks"),
    "FG3MLeaders": ("FG3M", "three-pointers made"),
}


def leaders_to_facts(datasets: dict[str, list[dict]]) -> list[dict]:
    """Turn AllTimeLeadersGrids result sets (lists of row dicts) into facts."""
    facts = []
    for set_name, (col, label) in LEADER_STATS.items():
        for row in datasets.get(set_name, []):
            name, value, rank = row.get("PLAYER_NAME"), row.get(col), row.get(f"{col}_RANK")
            if not name or value is None or rank is None:
                continue
            active = str(row.get("IS_ACTIVE_FLAG", "")).upper() == "Y"
            facts.append(validate_fact({
                "id": f"alltime_{col.lower()}_{int(rank)}_{row.get('PLAYER_ID', name)}",
                "text": (f"{name} ranks #{int(rank)} in NBA history in career {label} with "
                         f"{int(value):,}" + (" and is still active." if active else ".")),
                "player": name, "category": "career_leader", "stat": label,
                "rank": int(rank), "active": active,
            }, f"leaders:{set_name}"))
    return facts


def fetch_all_time_leaders() -> list[dict]:
    """Network call (stats.nba.com via nba_api). Returns facts; raises on failure."""
    from nba_api.stats.endpoints import alltimeleadersgrids

    result = alltimeleadersgrids.AllTimeLeadersGrids(per_mode_simple="Totals", season_type="Regular Season")
    return leaders_to_facts({name: _rows(result, name) for name in LEADER_STATS})


def _rows(endpoint, set_name: str) -> list[dict]:
    for rs in endpoint.get_dict()["resultSets"]:
        if rs["name"] == set_name:
            return [dict(zip(rs["headers"], row)) for row in rs["rowSet"]]
    return []
