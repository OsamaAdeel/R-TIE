#!/usr/bin/env bash
# RTIE backend entrypoint. Waits for Redis and Postgres to accept TCP
# connections, then execs the ASGI server. The first-run indexer is NOT
# orchestrated here — src/main.py's lifespan() already runs load_all_functions
# and IndexerAgent.index_all_loaded(force=False) on every startup, which is
# idempotent against a warm Redis volume. Cold volume = full bootstrap
# (~5-30 min); warm volume = seconds.
set -euo pipefail

REDIS_HOST="${REDIS_HOST:-rtie-redis}"
REDIS_PORT="${REDIS_PORT:-6379}"
POSTGRES_HOST="${POSTGRES_HOST:-rtie-postgres}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"

WAIT_TIMEOUT="${WAIT_TIMEOUT:-60}"

wait_for_tcp() {
    local name="$1" host="$2" port="$3"
    local elapsed=0
    echo "[entrypoint] waiting for ${name} at ${host}:${port} (timeout ${WAIT_TIMEOUT}s)..."
    while ! (echo > "/dev/tcp/${host}/${port}") >/dev/null 2>&1; do
        if [ "${elapsed}" -ge "${WAIT_TIMEOUT}" ]; then
            echo "[entrypoint] FATAL: ${name} did not become reachable at ${host}:${port} within ${WAIT_TIMEOUT}s" >&2
            exit 1
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    echo "[entrypoint] ${name} is up (${elapsed}s)"
}

wait_for_tcp "redis"    "${REDIS_HOST}"    "${REDIS_PORT}"
wait_for_tcp "postgres" "${POSTGRES_HOST}" "${POSTGRES_PORT}"

cat <<'BANNER'
[entrypoint] ----------------------------------------------------------------
[entrypoint] Starting RTIE backend.
[entrypoint] On a COLD Redis volume the lifespan will load the corpus graph
[entrypoint] and build the vector index. Expect ~5-30 min of startup work
[entrypoint] before /health returns ready. Stream logs with:
[entrypoint]     docker compose logs -f rtie-backend
[entrypoint] On a WARM Redis volume startup is seconds (load/index are
[entrypoint] idempotent and skip already-cached functions).
[entrypoint] ----------------------------------------------------------------
BANNER

exec "$@"
