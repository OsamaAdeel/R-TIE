# RTIE backend image. Multi-stage so Poetry, gcc, and libpq-dev never ship
# in the runtime layer. Pure pip/poetry install — Oracle access is thin-mode
# (no Instant Client required; verified via `grep init_oracle_client` returning
# zero matches across src/).

# ---------- Stage 1: builder ----------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VERSION=2.3.3 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "poetry==${POETRY_VERSION}"

WORKDIR /build

# Install deps before copying source so source edits don't bust the dep layer.
COPY pyproject.toml poetry.lock ./
# Strict install: Poetry 2.x errors on lockfile / pyproject.toml drift by
# default, which is what we want for image reproducibility.
RUN poetry install --only main --no-root --no-ansi

# ---------- Stage 2: runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

# libpq5: runtime library psycopg needs (without the -dev headers).
# curl: HEALTHCHECK probe.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the prebuilt venv from the builder stage.
COPY --from=builder /build/.venv /app/.venv

# Application source + corpus + config. The corpus (db/modules/) is baked
# in — the image IS the corpus delivery vehicle for v1 internal-Techlogix
# deployment. db/memory/create_memory_tables.sql is bind-mounted into the
# Postgres container by docker-compose, but we ship it here too so the
# backend image is self-contained for future deploy modes.
COPY src/ ./src/
COPY db/ ./db/
COPY config/ ./config/
COPY run.py ./
COPY deploy/backend/entrypoint.sh ./entrypoint.sh

# entrypoint must be executable on Linux even when copied from a Windows
# checkout where the executable bit isn't preserved.
RUN chmod +x ./entrypoint.sh && \
    # Strip CRLF line endings (Windows host -> Linux container).
    sed -i 's/\r$//' ./entrypoint.sh

EXPOSE 8000

# start-period gives the cold-start indexer headroom (5-30 min on first boot
# against an empty Redis volume). Once the volume is warm, /health responds
# within a few seconds and the healthcheck stabilises long before retries are
# exhausted.
HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=20 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["python", "run.py"]
