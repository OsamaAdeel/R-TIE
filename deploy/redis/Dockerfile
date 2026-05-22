# Pre-warmed Redis Stack image for RTIE.
#
# Bundles the indexed corpus (graph + RediSearch vector index) so teammates
# skip the 5-30 minute cold-start indexing on first boot. Built and pushed
# by scripts/publish_redis_image.ps1, which captures a fresh BGSAVE from
# the running rtie-redis container into ./dump.rdb before invoking build.
#
# How the warming actually reaches the running container: docker-compose
# mounts the named volume `redis_data` at /data. On a FRESH volume, Docker
# copies the image's /data contents into the volume — so the prewarmed
# dump.rdb lands there and redis-server loads it at startup. On a WARM
# volume (e.g. the maintainer's machine after re-indexing locally), the
# existing volume contents win — the image's baked-in dump.rdb is shadowed,
# and the local index is preserved.
FROM redis/redis-stack:latest

# uid 999 / gid 1000 match the `redis` user the base image runs as.
COPY --chown=999:1000 dump.rdb /data/dump.rdb
