"""RTIE CLI — Test the semantic search pipeline directly.

Usage:
    python cli.py index                    Index loader-validated functions (default; safe)
    python cli.py index --force            Re-embed loader-validated functions (skip cache)
    python cli.py index --resume           Resume an interrupted index + repair aggregates
    python cli.py index --from-disk        Walk db/modules/* on disk (W93b opt-in)
    python cli.py index --from-disk --force  Same, forcing re-embed
    python cli.py status                   Check index status
    python cli.py ask "your question"      Ask a question

Notes:
    The default `index` path reads loader-populated ``graph:<schema>:<fn>``
    keys from Redis (same path the backend lifespan uses at startup) — it
    requires the loader to have run at least once. Start the backend via
    ``python run.py`` before first use.

    ``--resume`` is the interrupted-run recovery path. Its per-function
    half is the existing skip-on-unchanged pass (re-embeds only missing /
    changed / previously-failed functions; ``approved`` docs with a
    matching source hash are skipped), so it picks up exactly where an
    interrupted full build stopped instead of restarting the 5-6 hr run.
    It then reconciles the per-schema aggregates: if ``graph:full`` /
    ``graph:index`` are missing or degenerate (the partial-aggregate state
    an interruption can leave), it rebuilds them atomically from the
    per-function graphs already in Redis. ``--resume`` does NOT re-parse
    per-function graphs (those are loader-owned and survive Redis
    restarts) and is mutually exclusive with ``--force``.

    ``--from-disk`` walks ``db/modules/*`` directly and embeds every .sql
    file, including functions the loader rejected. Pre-W93b default — kept
    as an opt-in for rebuilds outside the loader's view; not recommended
    for normal use.
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv(".env.dev")


async def get_clients():
    """Initialize Redis clients."""
    from src.tools.vector_store import VectorStore
    from src.tools.cache_tools import CacheClient

    vs = VectorStore(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
    )
    await vs.connect()
    await vs.ensure_index()

    cache = CacheClient(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        key_prefix="rtie",
    )
    await cache.connect()

    return vs, cache


async def cmd_index(
    force: bool = False,
    from_disk: bool = False,
    only_failed: bool = False,
    resume: bool = False,
):
    """Index functions for semantic search.

    Default (`from_disk=False`) — calls :meth:`IndexerAgent.index_all_loaded`,
    the same Phase-3 path the backend lifespan uses ([main.py:562](src/main.py)).
    Scans ``graph:<schema>:<fn>`` keys in Redis so the indexed corpus exactly
    matches the corpus the rest of RTIE serves answers from. Loader must have
    run at least once (start the backend via ``python run.py`` first).

    ``from_disk=True`` (W93b opt-in) — calls :meth:`IndexerAgent.index_all_modules`,
    the pre-W93b path that walks ``db/modules/*`` and embeds every .sql file,
    including functions the loader rejected. Kept as an escape hatch for
    rebuilds outside the loader's view; not the recommended default.

    ``only_failed=True`` (W122-recovery) — narrows the candidate set to
    functions whose existing vector doc is ``status="failed"``. Skips
    approved docs and functions without any existing doc. Use for
    targeted retry of a known-failed cohort (e.g., 86 LengthFinishReason-
    Error failures from W122d's first attempt) without re-touching the
    approved corpus. Ignored when ``from_disk=True``.

    ``resume=True`` — interrupted-run recovery. Runs the per-function
    pass with ``force=False`` (the existing skip-on-unchanged check is
    the resumability primitive: ``approved`` docs with a matching
    source hash are skipped, so only missing / changed / failed
    functions are re-embedded), then reconciles each schema's
    ``graph:full`` / ``graph:index`` aggregates — rebuilding them
    atomically from the per-function graphs in Redis when they are
    missing or degenerate. Mutually exclusive with ``force`` and
    ignored when ``from_disk=True``.
    """
    from src.agents.indexer import IndexerAgent
    from src.llm_factory import get_default_provider, resolve_embedding_config

    vs, _ = await get_clients()

    # Same resolver path as the backend lifespan (src/main.py).
    _emb_cfg = resolve_embedding_config()
    indexer = IndexerAgent(
        vector_store=vs,
        embedding_model=_emb_cfg["model"],
        llm_provider=get_default_provider(),
        llm_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    )

    if from_disk:
        print(
            "Indexing db/modules/* from disk "
            "(--from-disk; includes loader-rejected files)..."
        )
        result = await indexer.index_all_modules(force=force)

        for module, info in result.get("results", {}).items():
            print(f"\n  Module: {module}")
            print(f"  Total: {info.get('total_functions', 0)}")
            print(f"  Indexed: {info.get('indexed', 0)}")
            print(f"  Skipped: {info.get('skipped', 0)}")
            print(f"  Errors: {info.get('errors', 0)}")
            if info.get("indexed_functions"):
                print(f"  Indexed: {', '.join(info['indexed_functions'])}")
            if info.get("error_details"):
                for e in info["error_details"]:
                    print(f"  ERROR: {e['name']} - {e['error']}")

        await vs.close()
        return

    # Default path: loader-validated.
    import redis as _redis

    graph_redis = _redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
    )

    if resume:
        print(
            "Resuming index (--resume) - skip-on-unchanged per-function "
            "pass, then aggregate reconcile..."
        )
    elif only_failed:
        print(
            "Indexing loader-validated functions, only_failed=True "
            "(W122-recovery - targets status=failed docs only)..."
        )
    else:
        print("Indexing loader-validated functions (graph:<schema>:<fn>)...")
    # --resume's per-function half IS the existing skip-on-unchanged pass:
    # force=False so approved+matching-hash docs are skipped and only
    # missing / changed / failed functions are re-embedded.
    result = await indexer.index_all_loaded(
        graph_redis_client=graph_redis,
        force=force,
        only_failed=only_failed,
    )

    per_schema = result.get("results") or {}
    if not per_schema:
        print(
            "\n  No schemas discovered - no graph:<schema>:<fn> keys in Redis.\n"
            "  Run the backend at least once (`python run.py`) to load functions,\n"
            "  or use `python cli.py index --from-disk` to walk db/modules/* directly."
        )
    else:
        for schema, info in per_schema.items():
            indexed = info.get("indexed", 0)
            skipped = info.get("skipped", 0)
            errors = info.get("errors", 0)
            print(
                f"\n  Auto-index {schema}: "
                f"{indexed} indexed, {skipped} skipped, {errors} errors"
            )
            if resume:
                # Reconciliation summary derived from the per-function
                # pass: done = newly-indexed + already-done (skipped);
                # remaining = still-failing (errors). No extra Redis scan.
                considered = indexed + skipped + errors
                done = indexed + skipped
                print(
                    f"    vec docs: {done}/{considered} done"
                    + (f", {errors} still failing" if errors else "")
                )

    # --resume aggregate reconcile: detect + atomically rebuild any
    # missing/degenerate graph:full / graph:index from the per-function
    # graphs already in Redis. The common recovery case (per-fn work
    # complete, aggregates were the casualty) is handled here.
    if resume:
        from src.parsing.schema_discovery import discovered_schemas
        from src.parsing.aggregate_builder import reconcile_aggregates

        print("\n  Reconciling aggregates (graph:full / graph:index)...")
        for schema in discovered_schemas(graph_redis):
            outcome = reconcile_aggregates(graph_redis, schema)
            if outcome.get("action") == "rebuilt":
                rb = outcome.get("rebuild") or {}
                print(
                    f"    {schema}: REBUILT - {outcome['reason']} "
                    f"-> graph:full now {rb.get('function_count', 0)} functions, "
                    f"{rb.get('node_count', 0)} nodes"
                )
            else:
                print(f"    {schema}: ok - {outcome['reason']}")

    try:
        graph_redis.close()
    except Exception:
        pass
    await vs.close()


async def cmd_status():
    """Check index status."""
    vs, _ = await get_clients()
    stats = await vs.get_index_stats()
    print(f"Index: {stats.get('index_name', 'N/A')}")
    print(f"Documents: {stats.get('num_docs', 0)}")
    print(f"Records: {stats.get('num_records', 0)}")

    functions = await vs.list_indexed_functions()
    if functions:
        print(f"\nIndexed functions ({len(functions)}):")
        for fn in sorted(functions):
            print(f"  - {fn}")

    await vs.close()


async def cmd_ask(question: str):
    """Ask a question using the semantic search pipeline."""
    from langchain_openai import OpenAIEmbeddings
    from src.llm_factory import create_llm
    from src.agents.indexer import IndexerAgent
    from langchain_core.messages import SystemMessage, HumanMessage

    vs, cache = await get_clients()

    # Step 1: Classify query
    print(f"\n{'='*60}")
    print(f"Question: {question}")
    print(f"{'='*60}")

    # Step 2: Embed and search (vector) + keyword boost
    print("\n[1/4] Embedding query and searching Redis...")
    embeddings = OpenAIEmbeddings(model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"))
    query_vec = await embeddings.aembed_query(question)
    vector_results = await vs.search(query_embedding=query_vec, top_k=10)

    # Keyword boost: re-rank results that mention query terms in description/columns
    query_upper = question.upper()
    query_words = [w for w in query_upper.split() if len(w) > 3]

    for r in vector_results:
        keyword_hits = 0
        text = f"{r.get('description', '')} {r.get('key_columns', '')} {r.get('tables_written', '')}".upper()
        for word in query_words:
            if word in text:
                keyword_hits += 1
        # Lower score = better in cosine distance, so subtract bonus
        r["boosted_score"] = r["score"] - (keyword_hits * 0.15)

    results = sorted(vector_results, key=lambda r: r["boosted_score"])[:5]

    if not results:
        print("  No results found! Make sure functions are indexed (python cli.py index)")
        await vs.close()
        return

    print(f"  Found {len(results)} relevant functions:")
    for r in results:
        print(f"    - {r['function_name']} (vec: {r['score']:.4f}, boosted: {r['boosted_score']:.4f})")

    # Step 3: Fetch source code for each function
    print("\n[2/4] Fetching source code...")
    from src.agents.metadata_interpreter import _scan_modules_for_file, _read_sql_file

    multi_source = {}
    for r in results:
        fn_name = r["function_name"]
        filepath = _scan_modules_for_file(fn_name)
        if filepath:
            lines = _read_sql_file(filepath)
            source_text = "".join(
                line["text"] if isinstance(line, dict) else str(line) for line in lines
            )
            multi_source[fn_name] = {
                "source": source_text,
                "description": r.get("description", ""),
                "tables_read": r.get("tables_read", ""),
                "tables_written": r.get("tables_written", ""),
                "score": r["score"],
            }
            print(f"    {fn_name}: {len(lines)} lines loaded")
        else:
            print(f"    {fn_name}: FILE NOT FOUND")

    if not multi_source:
        print("  No source code found!")
        await vs.close()
        return

    # Step 4: Send to OpenAI gpt-4o for analysis
    print(f"\n[3/4] Analyzing {len(multi_source)} functions via gpt-4o...")

    llm = create_llm(provider="openai", model="gpt-4o", temperature=0, max_tokens=2000)

    per_function_answers = []

    for fn_name, data in multi_source.items():
        # Send only description + first 100 lines to keep payload small
        source_lines = data["source"].split("\n")
        truncated = "\n".join(source_lines[:100])
        if len(source_lines) > 100:
            truncated += f"\n... ({len(source_lines) - 100} more lines truncated)"

        prompt = (
            f"Question: {question}\n\n"
            f"Function: {fn_name}\n"
            f"Description: {data['description']}\n"
            f"Tables Read: {data['tables_read']}\n"
            f"Tables Written: {data['tables_written']}\n\n"
            f"Source (first 100 lines):\n{truncated}\n\n"
            f"If this function is relevant to the question, explain how. "
            f"If not relevant, say 'NOT RELEVANT' and nothing else."
        )

        try:
            resp = await llm.ainvoke([HumanMessage(content=prompt)])
            answer = resp.content.strip()
            if "NOT RELEVANT" not in answer.upper():
                per_function_answers.append(f"### {fn_name}\n{answer}")
                print(f"    {fn_name}: relevant")
            else:
                print(f"    {fn_name}: not relevant (skipped)")
        except Exception as e:
            print(f"    {fn_name}: ERROR - {e}")

    # Combine answers
    if per_function_answers:
        combined = "\n\n".join(per_function_answers)
    else:
        combined = "None of the found functions appear directly relevant to the question."

    class _Msg:
        content = f"## Answer: {question}\n\n{combined}"
    response = _Msg()

    print(f"\n[4/4] Answer:")
    print(f"{'='*60}")
    print(response.content)
    print(f"{'='*60}")

    await vs.close()
    await cache.close()


async def main():
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        return

    cmd = args[0]

    if cmd == "index":
        if "--help" in args or "-h" in args:
            print(__doc__)
            return
        force = "--force" in args
        from_disk = "--from-disk" in args
        only_failed = "--only-failed" in args
        resume = "--resume" in args
        if force and resume:
            print(
                "Error: --force and --resume are mutually exclusive.\n"
                "  --force re-embeds every function (clean re-build).\n"
                "  --resume skips already-done functions and repairs "
                "aggregates (interrupted-run recovery)."
            )
            return
        await cmd_index(
            force=force,
            from_disk=from_disk,
            only_failed=only_failed,
            resume=resume,
        )
    elif cmd == "status":
        await cmd_status()
    elif cmd == "ask" and len(args) > 1:
        question = " ".join(args[1:])
        await cmd_ask(question)
    else:
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
