"""W147 read-only probe — confirms the W49 PARTIAL_SOURCE_INDEXED false positive.

Read-only: issues Redis GETs only. No writes, no pipeline, no backend restart.
Reproduces detect_partial_source_function returning True for a function whose
body genuinely exists in graph:source: but was not retrieved into multi_source.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import redis

from src.parsing.store import get_parse_metadata, get_raw_source
from src.agents.logic_explainer import detect_partial_source_function

FN = "FN_G_TEST_CSTM"
SCHEMA = "OFSERM"

# Graph Redis: same host/port the loader uses (docker-compose Redis Stack).
r = redis.Redis(host="localhost", port=6379)

meta = get_parse_metadata(r, SCHEMA, FN)
src = get_raw_source(r, SCHEMA, FN)

print(f"meta present: {meta is not None}")
print(f"source lines: {len(src) if src else 0}")

# Simulate the failing call: the function never entered multi_source, so
# retrieved_source is None even though the body exists in Redis.
fires = detect_partial_source_function(
    function_name=FN,
    schema=SCHEMA,
    retrieved_source=None,
    redis_client=r,
)
print(f"W49 fires?   : {fires}")
