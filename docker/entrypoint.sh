#!/bin/sh
# Container entrypoint: seed the RAG database on first start, then run the booth.
# Extra arguments are passed to main.py (e.g. `docker run ... sports-booth --demo`).
set -eu

# Fail fast on bad configuration (missing API key, unauthenticated network bind, typos) before
# spending time seeding the database.
if ! python main.py --check-config "$@" > /tmp/config-check.txt 2>&1; then
    cat /tmp/config-check.txt >&2
    exit 2
fi

if [ "${BOOTH_SEED_ON_START:-1}" = "1" ] && [ ! -e "${BOOTH_RAG_DB:-/data/chroma_db}/chroma.sqlite3" ]; then
    echo "First start: seeding the historical facts database (downloads a ~90 MB embedding model once)…"
    if ! python rag/seed.py; then
        echo "WARNING: seeding failed. The Historian will report data unavailable until it succeeds;" >&2
        echo "         re-run with network access or: docker exec <container> python rag/seed.py" >&2
    fi
fi

exec python main.py "$@"
