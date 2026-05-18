# W93c — `cli.py index` cold-start ergonomics (backlog)

**Status:** Logged, not started
**Parent:** W93b (CLI default switched to `index_all_loaded`, merged 2026-05-18)
**Related:** W93 (indexer validation gate, merged at `51a0ed9`)
**Pattern parallel:** Ergonomics follow-up to W93b. W93b stopped the disk-walk-by-default footgun; W93c addresses the next tier: the default path produces a no-op when Redis is empty.

---

## Why this exists

W93b switched `python cli.py index` to `index_all_loaded`, which scans `graph:<schema>:<fn>` keys the loader populates. If Redis is empty (fresh container, wiped volume, never-run-backend dev box), the CLI prints:

```
  No schemas discovered — no graph:<schema>:<fn> keys in Redis.
  Run the backend at least once (`python run.py`) to load functions,
  or use `python cli.py index --from-disk` to walk db/modules/* directly.
```

…and exits with no work done. That's correct and actionable — but it's two steps where the user expected one. The pre-W93b `index_all_modules` path produced **something** (even if that something was a corpus-polluting disk walk) on a cold Redis.

## Scope

Make `python cli.py index` invoke the loader before the indexer when no schemas are discovered. Two implementation paths to weigh:

### Option A — fall-through invocation

If `index_all_loaded` returns zero schemas, run `load_all_functions(...)` then retry. Keeps the default ergonomic single-command. Mirror the lifespan's setup ([src/main.py:332-339](../src/main.py#L332-L339)): `redis.Redis(host, port)` graph client, `discover_module_folders()`, `load_all_functions(redis_client=..., schemas=...)`.

```python
# inside cmd_index, after the empty-result branch:
print("Loading functions from db/modules/* (loader has not run)...")
from src.parsing.loader import load_all_functions, discover_module_folders
load_all_functions(
    redis_client=graph_redis,
    module_folders=discover_module_folders(),
)
print("Retrying index_all_loaded...")
result = await indexer.index_all_loaded(graph_redis_client=graph_redis, force=force)
# fall through to per-schema summary print
```

### Option B — explicit `--bootstrap` flag

Same code but behind an opt-in flag: `python cli.py index --bootstrap` runs loader + indexer in sequence. Default stays as it is in W93b. Lower magic, but two-command for cold-start.

**Tentative pick:** Option A. The whole point of the CLI is "do the thing"; printing actionable error text when we *could* just do the thing is paternalistic for an internal tool. Option B is the right shape only if `load_all_functions` has irrecoverable failure modes that warrant explicit consent (currently it doesn't — failures are per-manifest and logged).

## Non-goals

- **Does not change the lifespan path.** The backend at `python run.py` already runs the loader at startup; no change to that flow.
- **Does not silently switch to `--from-disk`.** The W93b corpus-pollution avoidance must hold — Option A invokes the loader and re-runs `index_all_loaded`, not `index_all_modules`.
- **Does not add argparse.** `cli.py` is intentionally minimal (positional commands + `--flag in args` parsing). If Option B wins, the flag plumbing stays in `main()`.
- **Does not promise feature parity with the backend lifespan.** The lifespan does extra setup (schema snapshot prime, BI literal index, schema-aware caches) — out of scope here. The CLI's job is to populate the embedding corpus, not to warm every cache the backend uses.

## Estimated cost

- Code change: ~15 lines in [cli.py](../cli.py).
- Test: extend [tests/unit/test_cli_index_surface.py](../tests/unit/test_cli_index_surface.py) — mock `index_all_loaded` to return empty results, assert loader invocation occurs (Option A) or assert `--bootstrap` flag routes correctly (Option B).
- Wall-clock for cold-start run: ~5-30 minutes (the loader's per-function parsing dominates; matches lifespan startup time).
- Risk: low — the loader is already exercised at every backend startup; this is a re-entry from a different caller.

## Pre-condition

W93b must remain in place — both the default switch and the `--from-disk` opt-in. W93c is additive: when the default path finds no work, *then* the loader runs.

## Why a separate ticket

W93b's scope was the disk-walk-by-default footgun. Conflating the cold-start ergonomic with that fix would have widened the diff into loader invocation territory and crossed the "one ticket = one risk surface" line. W93c is the ergonomic polish that becomes worth doing once someone actually hits the empty-Redis case in normal use.

## Not blocking

W93c is logged but not urgent. The current behavior is correct and actionable — it just makes the user run two commands instead of one in a niche scenario. If it bites someone in the next few weeks, prioritize; if no one hits it, the doc stays as the placeholder until convenient.
