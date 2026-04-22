#!/usr/bin/env python3
"""NBA stats MCP server — wraps nba_api for live game data and advanced metrics."""
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("nba-stats")


@mcp.tool()
def get_live_scoreboard() -> str:
    """Get today's NBA live scoreboard with all game scores and status."""
    try:
        from nba_api.live.nba.endpoints import scoreboard as live_sb

        board = live_sb.ScoreBoard()
        games = board.games.get_dict()
        if not games:
            return json.dumps({"note": "No live NBA games right now.", "games": []})
        # Trim to relevant fields
        trimmed = []
        for g in games:
            trimmed.append({
                "gameId": g.get("gameId"),
                "homeTeam": g.get("homeTeam", {}).get("teamTricode"),
                "awayTeam": g.get("awayTeam", {}).get("teamTricode"),
                "homeScore": g.get("homeTeam", {}).get("score"),
                "awayScore": g.get("awayTeam", {}).get("score"),
                "period": g.get("period"),
                "gameClock": g.get("gameClock"),
                "gameStatus": g.get("gameStatusText"),
            })
        return json.dumps(trimmed, indent=2)
    except Exception as e:
        return json.dumps({
            "note": f"Live data unavailable ({e}), returning mock game",
            "games": [{
                "gameId": "0022401001",
                "homeTeam": "LAL",
                "awayTeam": "BOS",
                "homeScore": 87,
                "awayScore": 79,
                "period": 3,
                "gameClock": "4:23",
                "gameStatus": "3rd Qtr",
            }],
        }, indent=2)


@mcp.tool()
def get_boxscore(game_id: str) -> str:
    """Get advanced boxscore stats for a game (eFG%, TS%, pace, plus/minus)."""
    try:
        from nba_api.stats.endpoints import boxscoreadvancedv3

        box = boxscoreadvancedv3.BoxScoreAdvancedV3(game_id=game_id)
        home = box.home_team_stats.get_dict()
        away = box.away_team_stats.get_dict()
        players_df = box.player_stats.get_data_frame()
        top_players = players_df.head(8)[
            [c for c in ["playerName", "teamTricode", "minutes", "points",
                          "plusMinusPoints", "trueShootingPercentage",
                          "effectiveFieldGoalPercentage", "netRating"]
             if c in players_df.columns]
        ].to_dict(orient="records")
        return json.dumps({"home": home, "away": away, "topPlayers": top_players}, indent=2)
    except Exception as e:
        return json.dumps({
            "note": f"Boxscore unavailable ({e}), returning mock data",
            "home": {
                "teamTricode": "LAL",
                "effectiveFieldGoalPercentage": 0.542,
                "trueShootingPercentage": 0.601,
                "pace": 102.4,
                "offensiveRating": 118.3,
                "defensiveRating": 109.7,
            },
            "away": {
                "teamTricode": "BOS",
                "effectiveFieldGoalPercentage": 0.491,
                "trueShootingPercentage": 0.563,
                "pace": 102.4,
                "offensiveRating": 109.7,
                "defensiveRating": 118.3,
            },
            "topPlayers": [
                {"playerName": "LeBron James", "teamTricode": "LAL", "points": 28,
                 "plusMinusPoints": 18, "trueShootingPercentage": 0.684,
                 "effectiveFieldGoalPercentage": 0.611},
                {"playerName": "Anthony Davis", "teamTricode": "LAL", "points": 22,
                 "plusMinusPoints": 12, "trueShootingPercentage": 0.631,
                 "effectiveFieldGoalPercentage": 0.583},
                {"playerName": "Jayson Tatum", "teamTricode": "BOS", "points": 19,
                 "plusMinusPoints": -8, "trueShootingPercentage": 0.541,
                 "effectiveFieldGoalPercentage": 0.491},
            ],
        }, indent=2)


@mcp.tool()
def get_player_game_stats(game_id: str, player_name: str) -> str:
    """Get detailed stats for a specific player in a game."""
    try:
        from nba_api.stats.endpoints import boxscoreadvancedv3

        box = boxscoreadvancedv3.BoxScoreAdvancedV3(game_id=game_id)
        df = box.player_stats.get_data_frame()
        mask = df["playerName"].str.lower().str.contains(player_name.lower(), na=False)
        player_df = df[mask]
        if player_df.empty:
            return f"Player '{player_name}' not found in game {game_id}."
        return player_df.to_json(orient="records", indent=2)
    except Exception as e:
        return json.dumps({
            "note": f"Player data unavailable ({e}), returning mock",
            "player": player_name,
            "stats": {
                "points": 28, "assists": 9, "rebounds": 7,
                "plusMinusPoints": 18, "trueShootingPercentage": 0.684,
                "effectiveFieldGoalPercentage": 0.611, "netRating": 21.4,
                "note": "eFG% drops to 41.3% when Anthony Davis sits — significant lineup dependency",
            },
        }, indent=2)


@mcp.tool()
def get_team_lineup_impact(game_id: str, team_tricode: str) -> str:
    """Analyze how team performance changes with different lineup combinations."""
    try:
        from nba_api.stats.endpoints import boxscoreadvancedv3

        box = boxscoreadvancedv3.BoxScoreAdvancedV3(game_id=game_id)
        df = box.player_stats.get_data_frame()
        team_df = df[df["teamTricode"] == team_tricode.upper()]
        starters = team_df[team_df["startPosition"].notna() & (team_df["startPosition"] != "")]
        bench = team_df[team_df["startPosition"].isna() | (team_df["startPosition"] == "")]
        return json.dumps({
            "team": team_tricode,
            "starterNetRating": starters["netRating"].mean() if "netRating" in starters else "N/A",
            "benchNetRating": bench["netRating"].mean() if "netRating" in bench else "N/A",
            "starters": starters["playerName"].tolist() if "playerName" in starters else [],
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "note": f"Lineup data unavailable ({e}), returning mock",
            "team": team_tricode,
            "starterNetRating": 12.4,
            "benchNetRating": -6.8,
            "insight": f"The {team_tricode} bench unit is -19.2 net rating vs starters — massive drop-off",
        }, indent=2)


if __name__ == "__main__":
    mcp.run()
