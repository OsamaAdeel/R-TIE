"""Read-only scan: find a function with graph:meta but no/short graph:source
(a genuine partial-index state) so the W49 decline can be checked live."""
import redis

from src.parsing.store import get_raw_source
from src.parsing.serializer import from_msgpack  # noqa: F401  (sanity import)
from src.agents.logic_explainer import _retrieved_source_length, _PARTIAL_SOURCE_MIN_CHARS

r = redis.Redis(host="localhost", port=6379)

found = []
cursor = 0
scanned = 0
while True:
    cursor, keys = r.scan(cursor=cursor, match="graph:meta:*", count=500)
    for raw in keys:
        key = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        parts = key.split(":")
        if len(parts) != 4:
            continue
        _, _, schema, fn = parts
        scanned += 1
        body = get_raw_source(r, schema, fn)
        if _retrieved_source_length(body) < _PARTIAL_SOURCE_MIN_CHARS:
            found.append((schema, fn, _retrieved_source_length(body)))
    if cursor == 0:
        break

print(f"meta keys scanned: {scanned}")
print(f"genuine-partial (meta present, source < {_PARTIAL_SOURCE_MIN_CHARS}): {len(found)}")
for schema, fn, ln in found[:20]:
    print(f"  {schema}.{fn}  source_len={ln}")
