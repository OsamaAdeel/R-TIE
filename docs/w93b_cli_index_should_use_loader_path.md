# W93b — `cli.py index` should default to `index_all_loaded`

**Status:** RESOLVED 2026-05-18 — default switched to `index_all_loaded`; `--from-disk` preserved as opt-in. Closing entry in [RTIE_Weakness_Log.md](RTIE_Weakness_Log.md) under "W93b". Merge SHA pending.
**Discovered during:** W93 verification run (2026-05-16).

## The footgun

[cli.py:42-68](../cli.py#L42-L68) `cmd_index` constructs an `IndexerAgent` and calls `index_all_modules(force=force)`, which iterates `db/modules/*` and indexes every `.sql` file via `index_module`. This is **disk-walking** — it indexes the raw file set on disk, regardless of whether the loader accepted the function.

The Phase-3 path is `index_all_loaded`. From [indexer.py:246-249](../src/agents/indexer.py#L246-L249):

> *iterates `graph:<schema>:<fn>` keys directly — exactly matches the corpus the rest of RTIE already serves answers from.*

The two paths diverge sharply on OFSERM:
- Loader-validated corpus: 166 functions (per W35 Phase-3 manifest validation).
- Disk file set: ~280 `.sql` files in `ABL_CAR_CSTM_V4/functions/` alone.

So `cli.py index` repopulates the vector store with ~115 extra docs the loader explicitly rejected. Those docs have no `graph:<schema>:<fn>` backing, which means the rest of RTIE can't resolve them — but they still occupy slots in the KNN candidate set and can be returned as semantic-search hits.

## What happened in W93

The W93 verification script ran `index_module("ABL_CAR_CSTM_V4", force=False)` to re-attempt the 4 sentinel docs. The skip-if-unchanged logic spared the 165 already-approved docs, but it dutifully indexed all the ones with no existing vec doc — including ~115 loader-rejected functions. Corpus went 178 → 281 docs mid-run. I cleaned it up by deleting every `rtie:vec:OFSERM:<fn>` that lacked a corresponding `graph:OFSERM:<fn>` key.

## Fix shape

Change `cli.py:cmd_index` to:

1. Use the same Redis client and `index_all_loaded` path the lifespan uses ([main.py:562](../src/main.py#L562)). That requires constructing a `graph_redis_client` (`redis.Redis(host, port)`) and passing it through.
2. Optionally keep `index_all_modules` accessible behind a `--legacy-disk-walk` flag for the (rare) case where someone needs to index outside the loader's view.

Approximate diff:

```python
async def cmd_index(force: bool = False):
    from src.agents.indexer import IndexerAgent
    import redis as _redis

    vs, _ = await get_clients()
    graph_redis = _redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
    )

    indexer = IndexerAgent(
        vector_store=vs,
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        llm_provider="openai",
        llm_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    )

    print("Indexing loader-validated functions...")
    result = await indexer.index_all_loaded(
        graph_redis_client=graph_redis,
        force=force,
    )

    for schema, info in result.get("results", {}).items():
        print(f"\n  Schema: {schema}")
        print(f"  Indexed: {info.get('indexed', 0)}")
        print(f"  Skipped: {info.get('skipped', 0)}")
        print(f"  Errors:  {info.get('errors', 0)}")

    await vs.close()
```

Smoke test: `python cli.py index` against a fresh Redis should produce exactly the loader-validated corpus (OFSERM=166, OFSMDM=12), not the disk file set.

## Why a separate ticket

W93's scope was the indexer-state-lie gate. Conflating the CLI footgun with W93 would muddy the close-out — the gate is structurally correct regardless of whether the CLI uses disk-walk or loader-walk. W93b is a one-line behavior change to a thin wrapper; it warrants its own commit with its own smoke-test.
