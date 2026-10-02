import os

# Tests must never touch the real history database
os.environ.setdefault("BOOTH_HISTORY_DB", ":memory:")
