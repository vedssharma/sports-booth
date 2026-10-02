"""
Metadata filtering for the historical-facts store (pure helpers — no Chroma/embedding imports).

Semantic similarity alone is weak at "facts about THIS player": a query mentioning LeBron can match
a Kobe fact that sounds similar. Facts carry metadata (player, team, year, category), so tools
resolve the caller's wording ("LeBron", "LAL", "Lakers") to the exact values stored and pass a
`where` filter to Chroma.
"""
from booth.odds import TEAM_NAMES


def _nick(name: str) -> str:
    return name.lower().split()[-1] if name.split() else ""


def match_players(known: list[str], wanted: str) -> list[str]:
    """Stored player names matching `wanted`: exact (case-insensitive), then all-tokens, then a
    single token matching the first or last name."""
    w = wanted.lower().replace(".", "").split()
    if not w:
        return []
    exact = [k for k in known if k.lower() == wanted.lower().strip()]
    if exact:
        return exact
    out = []
    for k in known:
        tokens = k.lower().replace(".", "").split()
        if all(t in tokens for t in w) or (len(w) == 1 and w[0] in (tokens[0], tokens[-1])):
            out.append(k)
    return out


def match_teams(known: list[str], wanted: str) -> list[str]:
    """Stored team names matching `wanted`, which may be a tricode (LAL), nickname (Lakers) or
    full name. Matching is by nickname so franchise history works (Minneapolis Lakers ↔ Los
    Angeles Lakers)."""
    full = TEAM_NAMES.get(wanted.strip().upper(), wanted.strip())
    nick = _nick(full)
    return [k for k in known if _nick(k) == nick]


def build_where(players: list[str] | None = None, teams: list[str] | None = None,
                category: str = "", min_year: int = 0, max_year: int = 0) -> dict | None:
    """Chroma `where` clause for the given (already resolved) filters, or None for no filtering."""
    clauses: list[dict] = []
    if players:
        clauses.append({"player": players[0]} if len(players) == 1 else {"player": {"$in": players}})
    if teams:
        clauses.append({"team": teams[0]} if len(teams) == 1 else {"team": {"$in": teams}})
    if category:
        clauses.append({"category": category})
    if min_year:
        clauses.append({"year": {"$gte": int(min_year)}})
    if max_year:
        clauses.append({"year": {"$lte": int(max_year)}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}
