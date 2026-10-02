#
# Sports Booth container image.
#
#   docker build -t sports-booth .
#   docker run --rm -p 127.0.0.1:8000:8000 --env-file .env \
#       -e BOOTH_AUTH_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(24))') \
#       -v booth-data:/data sports-booth
#
# or simply `docker compose up` (see docker-compose.yml).
#
# Build args:
#   PYTHON_IMAGE   base image (default python:3.12-slim)
#   TORCH_BACKEND  "cpu" (default) installs CPU-only PyTorch, which keeps the image a few GB
#                  smaller than the CUDA wheels in uv.lock; anything else (e.g. "pypi") installs
#                  exactly what uv.lock pins.
ARG PYTHON_IMAGE=python:3.12-slim

# ── Build stage: resolve the locked dependencies into a virtualenv ────────────
FROM ${PYTHON_IMAGE} AS builder
ARG TORCH_BACKEND=cpu
ENV UV_NO_CACHE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir uv==0.8.17
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Export the exact locked set (no dev tools, no hashes so index swaps work), then install it.
# For the CPU build the CUDA runtime wheels are dropped and torch comes from the CPU index.
RUN uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.txt \
 && if [ "$TORCH_BACKEND" = "cpu" ]; then \
        grep -vE '^(nvidia-|triton)' requirements.txt > requirements.cpu.txt \
        && mv requirements.cpu.txt requirements.txt \
        && TORCH_FLAG="--torch-backend=cpu"; \
    else TORCH_FLAG=""; fi \
 && uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install $TORCH_FLAG -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE}
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # All mutable state lives under /data (a volume): history, odds snapshots, the RAG
    # database and the downloaded embedding model.
    BOOTH_HISTORY_DB=/data/history.db \
    BOOTH_ODDS_DB=/data/odds.db \
    BOOTH_RAG_DB=/data/chroma_db \
    HF_HOME=/data/huggingface \
    ANONYMIZED_TELEMETRY=False \
    # Inside a container the app must listen on all interfaces; access is restricted by how the
    # port is published (see docker-compose.yml) or by BOOTH_AUTH_TOKEN.
    BOOTH_HOST=0.0.0.0 \
    BOOTH_LOG_FORMAT=json

RUN useradd --system --uid 10001 --create-home --home-dir /home/booth booth \
 && mkdir -p /data && chown booth:booth /data
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY main.py pyproject.toml ./
COPY booth ./booth
COPY mcp_servers ./mcp_servers
COPY rag/__init__.py rag/facts.py rag/filters.py rag/seed.py ./rag/
COPY static ./static
COPY docker/entrypoint.sh /usr/local/bin/booth-entrypoint
RUN chmod 0755 /usr/local/bin/booth-entrypoint

USER booth
VOLUME /data
EXPOSE 8000
# Liveness only (/healthz is unauthenticated); /health has the details and needs the token.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('BOOTH_PORT', '8000'), timeout=4)"
ENTRYPOINT ["booth-entrypoint"]
CMD []
