"""RTIE CLI — Test the semantic search pipeline directly.

Usage:
    python cli.py index                    Index all modules
    python cli.py index --force            Force re-index all
    python cli.py status                   Check index status
    python cli.py ask "your question"      Ask a question
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


async def cmd_index(force: bool = False):
    """Index all modules."""
    from src.agents.indexer import IndexerAgent

    vs, _ = await get_clients()

    indexer = IndexerAgent(
        vector_store=vs,
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        llm_provider="openai",
        llm_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    )

    print("Indexing all modules...")
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
                print(f"  ERROR: {e['name']} — {e['error']}")

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

    # Step 4: Send to Ollama (local LLM) — no network size limits
    print(f"\n[3/4] Analyzing {len(multi_source)} functions via Ollama (local)...")

    from src.llm_factory import create_llm
    from langchain_core.messages import SystemMessage, HumanMessage

    llm = create_llm(provider="ollama", model=os.getenv("OLLAMA_MODEL", "llama3:8b"), temperature=0, max_tokens=2000)

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
            print(f"    {fn_name}: ERROR — {e}")

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
        force = "--force" in args
        await cmd_index(force=force)
    elif cmd == "status":
        await cmd_status()
    elif cmd == "ask" and len(args) > 1:
        question = " ".join(args[1:])
        await cmd_ask(question)
    else:
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
