"""
Seed the ChromaDB historical NBA database.
Run once before starting the booth:

    uv run python rag/seed.py
"""
from pathlib import Path
import chromadb
from sentence_transformers import SentenceTransformer

DB_PATH = str(Path(__file__).parent / "chroma_db")
COLLECTION_NAME = "nba_history"
EMBED_MODEL = "all-MiniLM-L6-v2"

HISTORICAL_FACTS = [
    # Scoring records
    {
        "id": "wilt_100_1962",
        "text": (
            "Wilt Chamberlain scored 100 points on March 2, 1962, "
            "for the Philadelphia Warriors vs New York Knicks in Hershey, PA. "
            "He went 36-63 from the field and 28-32 from the free-throw line."
        ),
        "player": "Wilt Chamberlain", "team": "Philadelphia Warriors",
        "year": 1962, "category": "scoring_record",
    },
    {
        "id": "kobe_81_2006",
        "text": (
            "Kobe Bryant scored 81 points on January 22, 2006, against the Toronto Raptors — "
            "the second-highest single-game total in NBA history. He scored 55 in the second half alone."
        ),
        "player": "Kobe Bryant", "team": "Los Angeles Lakers",
        "year": 2006, "category": "scoring_record",
    },
    {
        "id": "devin_booker_70_2017",
        "text": (
            "Devin Booker scored 70 points on March 24, 2017, becoming the youngest player "
            "(20 years old) to score 70+ in a game. The Suns lost to the Celtics 130-120."
        ),
        "player": "Devin Booker", "team": "Phoenix Suns",
        "year": 2017, "category": "scoring_record",
    },
    # Rookie milestones
    {
        "id": "magic_rookie_1980",
        "text": (
            "Magic Johnson averaged 18.0 pts, 7.7 reb, and 7.3 ast as a rookie in 1979-80 "
            "and famously started at center in Game 6 of the Finals, scoring 42 points "
            "to win the championship."
        ),
        "player": "Magic Johnson", "team": "Los Angeles Lakers",
        "year": 1979, "category": "rookie_performance",
    },
    {
        "id": "lebron_debut_2003",
        "text": (
            "LeBron James scored 25 points in his NBA debut on October 29, 2003, "
            "at age 18, making him one of the youngest players to score 25+ in a debut."
        ),
        "player": "LeBron James", "team": "Cleveland Cavaliers",
        "year": 2003, "category": "rookie_performance",
    },
    {
        "id": "oscar_robertson_triple_double_1962",
        "text": (
            "Oscar Robertson averaged a triple-double for an entire season in 1961-62: "
            "30.8 pts, 12.5 reb, 11.4 ast — a feat not matched until Russell Westbrook in 2016-17."
        ),
        "player": "Oscar Robertson", "team": "Cincinnati Royals",
        "year": 1962, "category": "season_record",
    },
    # Playoff performances
    {
        "id": "jordan_flu_game_1997",
        "text": (
            "Michael Jordan scored 38 points in the 1997 NBA Finals Game 5 while visibly ill "
            "with what became known as the 'Flu Game.' He collapsed into Scottie Pippen's arms "
            "after hitting the go-ahead three-pointer."
        ),
        "player": "Michael Jordan", "team": "Chicago Bulls",
        "year": 1997, "category": "playoff_performance",
    },
    {
        "id": "lebron_block_2016_finals",
        "text": (
            "LeBron James's chase-down block on Andre Iguodala in the final minute of Game 7 "
            "of the 2016 NBA Finals preserved a tie score and led to the Cavaliers' historic "
            "comeback from 3-1 down — the first team to do so in Finals history."
        ),
        "player": "LeBron James", "team": "Cleveland Cavaliers",
        "year": 2016, "category": "playoff_moment",
    },
    # Lakers history
    {
        "id": "lakers_showtime_1987",
        "text": (
            "The 1986-87 Lakers, led by Magic Johnson and Kareem Abdul-Jabbar, went 65-17 "
            "and won the championship in 6 games over the Celtics. Magic won Finals MVP "
            "averaging 26.2 pts, 8.0 ast, and 7.0 reb."
        ),
        "player": "Magic Johnson", "team": "Los Angeles Lakers",
        "year": 1987, "category": "championship",
    },
    {
        "id": "lakers_threepeat_2002",
        "text": (
            "The Lakers won three consecutive championships (2000-2002), becoming only the "
            "third franchise to three-peat. Shaquille O'Neal won Finals MVP all three years, "
            "averaging 38.5 pts and 16.7 reb in the 2000 Finals."
        ),
        "player": "Shaquille O'Neal", "team": "Los Angeles Lakers",
        "year": 2000, "category": "championship",
    },
    # Celtics history
    {
        "id": "celtics_bill_russell_championships",
        "text": (
            "Bill Russell won 11 NBA championships in 13 seasons with the Boston Celtics "
            "(1957-1969), including 8 consecutive titles from 1959-1966 — the longest "
            "dynasty in North American sports history."
        ),
        "player": "Bill Russell", "team": "Boston Celtics",
        "year": 1957, "category": "championship",
    },
    {
        "id": "celtics_bird_1986",
        "text": (
            "The 1985-86 Boston Celtics went 67-15 and are widely considered the greatest "
            "team in franchise history. Larry Bird averaged 25.8 pts, 9.8 reb, and 6.8 ast "
            "and won his third consecutive MVP."
        ),
        "player": "Larry Bird", "team": "Boston Celtics",
        "year": 1986, "category": "season_record",
    },
    # Closeout games / momentum
    {
        "id": "ray_allen_2013_finals_game6",
        "text": (
            "Ray Allen's corner three with 5.1 seconds left in Game 6 of the 2013 NBA Finals "
            "tied the game and saved the Heat from elimination. Miami won in OT and again in "
            "Game 7 to repeat as champions."
        ),
        "player": "Ray Allen", "team": "Miami Heat",
        "year": 2013, "category": "playoff_moment",
    },
    {
        "id": "pierce_paul_2008_finals",
        "text": (
            "Paul Pierce won 2008 Finals MVP after averaging 21.8 pts per game as the Celtics "
            "ended a 22-year title drought, defeating the Lakers 4-2. Pierce famously returned "
            "from what appeared to be a serious knee injury in Game 1."
        ),
        "player": "Paul Pierce", "team": "Boston Celtics",
        "year": 2008, "category": "playoff_performance",
    },
    # Scoring streaks
    {
        "id": "jordan_37_consecutive_1987",
        "text": (
            "Michael Jordan scored 37+ points in consecutive playoff games during the 1988 "
            "first round vs Cleveland, totaling 226 points in 10 games — a record-setting "
            "performance that began his legacy as the premier playoff scorer."
        ),
        "player": "Michael Jordan", "team": "Chicago Bulls",
        "year": 1988, "category": "playoff_scoring",
    },
    {
        "id": "kobe_streak_2006",
        "text": (
            "Kobe Bryant scored 40 or more points in four consecutive games in January 2006, "
            "including his 81-point game. He totaled 232 points over those four games — "
            "the most prolific four-game stretch in NBA history."
        ),
        "player": "Kobe Bryant", "team": "Los Angeles Lakers",
        "year": 2006, "category": "scoring_streak",
    },
    # Assists / playmaking
    {
        "id": "stockton_assists_record",
        "text": (
            "John Stockton holds the all-time NBA assist record with 15,806 career assists — "
            "nearly 4,000 more than second place Jason Kidd. He also holds the all-time steals "
            "record with 3,265."
        ),
        "player": "John Stockton", "team": "Utah Jazz",
        "year": 1990, "category": "career_record",
    },
    # Defense
    {
        "id": "dpoy_history",
        "text": (
            "Dikembe Mutombo won the Defensive Player of the Year award four times (1994, 1997, "
            "1998, 2001) — more than any player in history. He famously shook his finger after "
            "every blocked shot."
        ),
        "player": "Dikembe Mutombo", "team": "multiple",
        "year": 1994, "category": "defensive_record",
    },
]


def seed() -> None:
    print(f"Loading embedding model '{EMBED_MODEL}'…")
    embedder = SentenceTransformer(EMBED_MODEL)

    print("Connecting to ChromaDB…")
    client = chromadb.PersistentClient(path=DB_PATH)
    collection = client.get_or_create_collection(COLLECTION_NAME)

    existing = set(collection.get()["ids"] or [])
    to_add = [f for f in HISTORICAL_FACTS if f["id"] not in existing]

    if not to_add:
        print(f"Database already contains {collection.count()} facts. Nothing to add.")
        return

    print(f"Embedding {len(to_add)} historical facts…")
    texts = [f["text"] for f in to_add]
    embeddings = embedder.encode(texts, show_progress_bar=True).tolist()

    collection.add(
        ids=[f["id"] for f in to_add],
        embeddings=embeddings,
        documents=texts,
        metadatas=[
            {k: v for k, v in f.items() if k not in ("id", "text")}
            for f in to_add
        ],
    )
    print(f"✓ Seeded {len(to_add)} facts. Total in DB: {collection.count()}")


if __name__ == "__main__":
    seed()
