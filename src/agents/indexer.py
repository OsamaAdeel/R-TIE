"""
RTIE Indexer Agent.

Scans PL/SQL module directories, generates LLM-enriched descriptions
for each function, computes embeddings via OpenAI, and stores them
in the Redis vector store for semantic search. Supports incremental
indexing (skips unchanged source) and force re-indexing.
"""

import asyncio
import glob
import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from langchain_openai import OpenAIEmbeddings
from langchain_core.messages import SystemMessage, HumanMessage

from src.tools.vector_store import VectorStore
from src.llm_factory import create_llm
from src.llm_errors import categorize_llm_exception
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id
from src.parsing.keyspace import SchemaAwareKeyspace
from src.parsing.loader import _extract_schema_from_source
from src.parsing.manifest import load_manifest
from src.parsing.schema_discovery import discovered_schemas
from src.parsing.store import get_raw_source

logger = get_logger(__name__, concern="app")

# Same module paths as metadata_interpreter
_RTIE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
MODULES_DIRS = [
    os.path.join(_RTIE_ROOT, "db", "modules"),
]

DESCRIPTION_SYSTEM_PROMPT = """You are a PL/SQL documentation specialist for Oracle OFSAA regulatory systems.

Given a PL/SQL function's source code, produce a rich description optimized for semantic search.

Respond with ONLY valid JSON — no markdown, no extra text.

{
  "description": "A keyword-enriched natural language description (2-4 paragraphs) covering:
    - What the function does (purpose and business context)
    - Which tables it reads from and what data it extracts
    - Which tables it writes to and what operations (INSERT/UPDATE/DELETE/MERGE)
    - Key columns and calculations involved
    - Any regulatory or business domain context (Basel III, capital adequacy, operational risk, etc.)
    Include specific table names, column names, and business terms as keywords.",
  "tables_read": ["TABLE1", "TABLE2"],
  "tables_written": ["TABLE3"],
  "key_columns": ["COL1", "COL2"]
}

Rules:
- Include EVERY table name found in the source (FROM, JOIN, INTO, UPDATE, DELETE FROM, MERGE INTO)
- Include EVERY significant column name referenced in SELECT, INSERT, UPDATE, WHERE clauses
- Use business-domain vocabulary: operational risk, gross income, capital adequacy, GL data, product processor, exposure, provision, deduction ratio, beta factor, etc.
- The description must be findable by someone asking about any column, table, or business concept in the function
- Do NOT include the raw source code in the description
"""


class IndexerAgent:
    """Agent for indexing PL/SQL functions into the vector store.

    Scans module directories for .sql files, generates LLM-enriched
    descriptions, computes OpenAI embeddings, and stores everything
    in Redis for semantic search.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedding_model: str = "text-embedding-3-small",
        llm_provider: str = "openai",
        llm_model: str = "gpt-4o",
        temperature: float = 0,
        max_tokens: int = 2000,
    ) -> None:
        """Initialize the IndexerAgent.

        Args:
            vector_store: Redis vector store client.
            embedding_model: OpenAI embedding model name.
            llm_provider: LLM provider for description generation.
            llm_model: LLM model name for description generation.
            temperature: LLM temperature. Defaults to 0.
            max_tokens: Max tokens for description generation.
        """
        self._vector_store = vector_store
        self._embedding_model = embedding_model
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._temperature = temperature
        self._max_tokens = max_tokens
        import ssl as _ssl
        import httpx as _httpx
        _ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        _ssl_ctx.maximum_version = _ssl.TLSVersion.TLSv1_2
        _ssl_ctx.load_default_certs()
        self._embeddings = OpenAIEmbeddings(
            model=embedding_model,
            http_client=_httpx.Client(verify=_ssl_ctx, timeout=60),
            http_async_client=_httpx.AsyncClient(verify=_ssl_ctx, timeout=60),
        )

    async def index_module(
        self, module_name: str, force: bool = False
    ) -> Dict[str, Any]:
        """Index all PL/SQL functions in a module.

        Scans the module directory for .sql files, generates descriptions
        and embeddings, and stores them in the vector store. Skips functions
        whose source hasn't changed unless force=True.

        Args:
            module_name: Name of the module directory.
            force: If True, re-index all functions regardless of source hash.

        Returns:
            Dict with indexing results: status, counts, details.
        """
        correlation_id = get_correlation_id()
        logger.info(
            f"Indexing module: {module_name} force={force} | "
            f"correlation_id={correlation_id}"
        )

        functions = self._scan_module_functions(module_name)
        if not functions:
            return {
                "status": "error",
                "message": f"No functions found for module '{module_name}'",
                "module": module_name,
            }

        await self._vector_store.ensure_index()

        indexed, skipped, errors = [], [], []

        for fn_info in functions:
            fn_name = fn_info["name"]
            source_code = fn_info["source"]
            source_hash = self._compute_source_hash(source_code)
            # Phase 3: derive each function's owning Oracle schema so the
            # vector doc can be tagged correctly. The module folder might
            # mix schemas in theory; in practice every OFSAA module folder
            # is single-schema, but reading the CREATE OR REPLACE prefix
            # is just as cheap and keeps the wiring honest.
            fn_schema = _extract_schema_from_source(
                source_code.splitlines(keepends=True)
            ) or ""

            # Check if already indexed with same source. Pre-Phase-3 the
            # lookup keyed off (module, fn_name); Phase 3 keys off
            # (schema, fn_name) since the doc-key prefix moved.
            if not force and fn_schema:
                existing = await self._vector_store.get_function_doc(
                    fn_schema, fn_name
                )
                if existing and existing.get("source_hash") == source_hash:
                    skipped.append(fn_name)
                    logger.info(
                        f"Skipping {fn_name} — source unchanged | "
                        f"correlation_id={correlation_id}"
                    )
                    continue

            try:
                # Delay between functions to avoid rate limits
                if indexed or errors:
                    await asyncio.sleep(2)

                # Truncate source to keep payload under 2KB (corporate network TLS limit)
                max_chars = 3000  # ~750 tokens, keeps total request under 2KB
                truncated_source = source_code[:max_chars]
                if len(source_code) > max_chars:
                    truncated_source += f"\n\n-- [TRUNCATED: {len(source_code) - max_chars} more characters]"

                print(f"    Generating description for {fn_name} ({len(source_code)} chars, sending {len(truncated_source)})...")

                # Generate description via LLM
                desc_result = await self._generate_description(fn_name, truncated_source)

                # Small delay before embedding call
                await asyncio.sleep(1)

                # Generate embedding
                embedding = await self._get_embedding(desc_result["description"])

                # Store in vector store
                await self._vector_store.upsert_function(
                    module=module_name,
                    function_name=fn_name,
                    description=desc_result["description"],
                    embedding=embedding,
                    tables_read=desc_result.get("tables_read", []),
                    tables_written=desc_result.get("tables_written", []),
                    key_columns=desc_result.get("key_columns", []),
                    source_hash=source_hash,
                    status="approved",
                    schema=fn_schema,
                )
                indexed.append(fn_name)
                logger.info(
                    f"Indexed {fn_name} | correlation_id={correlation_id}"
                )
            except Exception as exc:
                errors.append({"name": fn_name, "error": str(exc)})
                logger.error(
                    f"Failed to index {fn_name}: {exc} | "
                    f"correlation_id={correlation_id}"
                )

        result = {
            "status": "completed",
            "module": module_name,
            "total_functions": len(functions),
            "indexed": len(indexed),
            "skipped": len(skipped),
            "errors": len(errors),
            "indexed_functions": indexed,
            "skipped_functions": skipped,
            "error_details": errors,
        }

        logger.info(
            f"Module indexing complete: {module_name} — "
            f"{len(indexed)} indexed, {len(skipped)} skipped, "
            f"{len(errors)} errors | correlation_id={correlation_id}"
        )
        return result

    async def index_all_loaded(
        self,
        graph_redis_client,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Index every function the loader populated, across every schema.

        Phase 3 startup path. Replaces the pre-Phase-3 ``auto_index_modules``
        loop, which read .sql files off disk and missed the manifest's
        active/inactive distinction (so it would have tried to embed the
        413 OFSERM .sql files that the loader rejected). This method
        iterates ``graph:<schema>:<fn>`` keys directly — exactly matches
        the corpus the rest of RTIE already serves answers from.

        For each function it:
          1. Reads source from ``graph:source:<schema>:<fn>`` (the
             loader's canonical source cache).
          2. Resolves the legacy ``module`` tag from the relevant
             manifest's batch field, so module-scoped admin queries
             keep working.
          3. Generates a description via :meth:`_generate_description`,
             embeds, and upserts with the schema TAG populated.

        Functions whose ``source_hash`` matches the existing indexed doc
        are skipped unless ``force=True``.

        Returns a per-schema results dict (counts indexed/skipped/errors)
        suitable for an info-level startup log line.
        """
        correlation_id = get_correlation_id()
        if graph_redis_client is None:
            logger.warning(
                "index_all_loaded: graph_redis_client is None; skipping "
                "(no schemas to iterate). | correlation_id=%s",
                correlation_id,
            )
            return {"status": "skipped", "reason": "no graph redis client"}

        await self._vector_store.ensure_index()
        function_to_module = self._build_function_to_module_map()
        schemas = discovered_schemas(graph_redis_client)
        per_schema_results: Dict[str, Dict[str, Any]] = {}

        for schema in schemas:
            indexed: List[str] = []
            skipped: List[str] = []
            errors: List[Dict[str, str]] = []

            try:
                pattern = SchemaAwareKeyspace.graph_scan_pattern(schema)
                raw_keys = graph_redis_client.keys(pattern) or []
            except Exception as exc:
                logger.warning(
                    "index_all_loaded: SCAN failed for %s: %s | correlation_id=%s",
                    schema, exc, correlation_id,
                )
                per_schema_results[schema] = {
                    "status": "error",
                    "error": f"scan failed: {exc}",
                    "indexed": 0, "skipped": 0, "errors": 0,
                }
                continue

            function_names: List[str] = []
            for raw in raw_keys:
                key = (
                    raw.decode("utf-8", errors="ignore")
                    if isinstance(raw, (bytes, bytearray))
                    else str(raw)
                )
                parsed = SchemaAwareKeyspace.parse_graph_key(key)
                if parsed is None or parsed[0] != schema:
                    continue
                function_names.append(parsed[1])

            logger.info(
                "index_all_loaded: %d function(s) to consider for %s",
                len(function_names), schema,
            )

            for fn_name in function_names:
                try:
                    raw_lines = get_raw_source(
                        graph_redis_client, schema, fn_name
                    )
                except Exception as exc:
                    errors.append({"name": fn_name, "error": f"read failed: {exc}"})
                    continue

                if not raw_lines:
                    # Loader recorded a graph but no source body — usually
                    # a manifest-listed inactive task. Skip rather than
                    # embed an empty description.
                    errors.append({"name": fn_name, "error": "no source body"})
                    continue

                source_code = "".join(
                    s.decode("utf-8", errors="replace")
                    if isinstance(s, (bytes, bytearray))
                    else str(s)
                    for s in raw_lines
                )
                source_hash = self._compute_source_hash(source_code)

                if not force:
                    existing = await self._vector_store.get_function_doc(
                        schema, fn_name
                    )
                    if existing and existing.get("source_hash") == source_hash:
                        skipped.append(fn_name)
                        continue

                module_tag = function_to_module.get(
                    (schema, fn_name.upper()), schema
                )

                try:
                    if indexed or errors:
                        await asyncio.sleep(2)

                    max_chars = 3000
                    truncated_source = source_code[:max_chars]
                    if len(source_code) > max_chars:
                        truncated_source += (
                            f"\n\n-- [TRUNCATED: "
                            f"{len(source_code) - max_chars} more characters]"
                        )

                    print(
                        f"    Generating description for {fn_name} "
                        f"({len(source_code)} chars, sending "
                        f"{len(truncated_source)})..."
                    )

                    desc_result = await self._generate_description(
                        fn_name, truncated_source
                    )
                    await asyncio.sleep(1)
                    embedding = await self._get_embedding(
                        desc_result["description"]
                    )
                    await self._vector_store.upsert_function(
                        module=module_tag,
                        function_name=fn_name,
                        description=desc_result["description"],
                        embedding=embedding,
                        tables_read=desc_result.get("tables_read", []),
                        tables_written=desc_result.get("tables_written", []),
                        key_columns=desc_result.get("key_columns", []),
                        source_hash=source_hash,
                        status="approved",
                        schema=schema,
                    )
                    indexed.append(fn_name)
                except Exception as exc:
                    errors.append({"name": fn_name, "error": str(exc)})
                    logger.error(
                        "Failed to index %s.%s: %s | correlation_id=%s",
                        schema, fn_name, exc, correlation_id,
                    )

            per_schema_results[schema] = {
                "status": "completed",
                "indexed": len(indexed),
                "skipped": len(skipped),
                "errors": len(errors),
                "error_details": errors,
            }
            logger.info(
                "index_all_loaded: %s — %d indexed, %d skipped, %d errors | "
                "correlation_id=%s",
                schema, len(indexed), len(skipped), len(errors), correlation_id,
            )

        return {
            "status": "completed",
            "schemas_processed": len(schemas),
            "results": per_schema_results,
        }

    def _build_function_to_module_map(self) -> Dict[Tuple[str, str], str]:
        """Return ``(schema, FN_UPPER) -> module_batch`` from every manifest.

        Used by :meth:`index_all_loaded` to populate the legacy ``module``
        TAG on each vector doc. Modules without a manifest contribute
        nothing — those functions get ``module=schema`` as a sensible
        fallback at the call site.
        """
        mapping: Dict[Tuple[str, str], str] = {}
        for modules_dir in MODULES_DIRS:
            if not os.path.isdir(modules_dir):
                continue
            for entry in sorted(os.listdir(modules_dir)):
                module_path = os.path.join(modules_dir, entry)
                if not os.path.isdir(module_path):
                    continue
                try:
                    manifest = load_manifest(module_path)
                except Exception as exc:
                    logger.debug(
                        "manifest load failed for %s: %s", module_path, exc
                    )
                    continue
                if manifest is None:
                    continue
                schema = (manifest.schema or "").upper()
                for task in manifest.iter_all_tasks():
                    fn_upper = task.name.strip().upper()
                    mapping[(schema, fn_upper)] = manifest.batch
        return mapping

    async def index_all_modules(self, force: bool = False) -> Dict[str, Any]:
        """Index all discovered modules.

        Args:
            force: If True, re-index all functions.

        Returns:
            Dict with results per module.
        """
        modules = self._discover_modules()
        results = {}
        for module_name in modules:
            results[module_name] = await self.index_module(module_name, force=force)
        return {
            "status": "completed",
            "modules_processed": len(modules),
            "results": results,
        }

    async def _generate_description(
        self, function_name: str, source_code: str
    ) -> Dict[str, Any]:
        """Generate a rich description of a PL/SQL function via LLM.

        Args:
            function_name: Name of the function.
            source_code: Full PL/SQL source code.

        Returns:
            Dict with description, tables_read, tables_written, key_columns.
        """
        # Use OpenAI for indexing (one-time, fast)
        llm = create_llm(
            provider="openai",
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            json_mode=True,
        )

        messages = [
            SystemMessage(content=DESCRIPTION_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"Generate a semantic search description for this PL/SQL function.\n\n"
                    f"Function Name: {function_name}\n\n"
                    f"Source Code:\n{source_code}"
                )
            ),
        ]

        # Indexer is an offline batch job, not user-facing — categorize +
        # log the LLM exception, fall back to an empty description so
        # indexing can continue with the next function rather than aborting
        # the whole batch. Mirrors the existing JSONDecodeError fallback below.
        try:
            response = await llm.ainvoke(messages)
        except Exception as exc:
            category, _ = categorize_llm_exception(exc)
            logger.exception(
                "Indexer LLM call failed for %s | category=%s",
                function_name, category,
            )
            return {
                "description": f"(indexing failed: {category})",
                "tables_read": [],
                "tables_written": [],
                "key_columns": [],
            }
        raw = response.content.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        # Try JSON parse; if LLM didn't return valid JSON, build a fallback
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Non-JSON response from LLM for {function_name}, using raw text as description")
            return {
                "description": raw[:2000],
                "tables_read": [],
                "tables_written": [],
                "key_columns": [],
            }

    async def _get_embedding(self, text: str) -> List[float]:
        """Generate an embedding vector for the given text.

        Args:
            text: Text to embed.

        Returns:
            List of floats (1536 dimensions).
        """
        return await self._embeddings.aembed_query(text)

    def _scan_module_functions(self, module_name: str) -> List[Dict[str, str]]:
        """Scan a module directory for all .sql files.

        Args:
            module_name: Name of the module directory.

        Returns:
            List of dicts with 'name' (function name) and 'source' (file content).
        """
        functions = []

        for modules_dir in MODULES_DIRS:
            if not os.path.isdir(modules_dir):
                continue

            # Search for exact module name or partial match
            for entry in os.listdir(modules_dir):
                entry_path = os.path.join(modules_dir, entry)
                if not os.path.isdir(entry_path):
                    continue
                if entry.upper() == module_name.upper() or module_name.upper() in entry.upper():
                    pattern = os.path.join(entry_path, "**", "*.sql")
                    for filepath in glob.glob(pattern, recursive=True):
                        fn_name = os.path.splitext(os.path.basename(filepath))[0].upper()
                        with open(filepath, "r", encoding="utf-8") as f:
                            source = f.read()
                        functions.append({"name": fn_name, "source": source})

        logger.info(f"Found {len(functions)} functions in module '{module_name}'")
        return functions

    def _discover_modules(self) -> List[str]:
        """Discover all module directories.

        Returns:
            List of module directory names.
        """
        modules = set()
        for modules_dir in MODULES_DIRS:
            if not os.path.isdir(modules_dir):
                continue
            for entry in os.listdir(modules_dir):
                entry_path = os.path.join(modules_dir, entry)
                if os.path.isdir(entry_path):
                    modules.add(entry)
        return sorted(modules)

    @staticmethod
    def _compute_source_hash(source_code: str) -> str:
        """Compute SHA256 hash of source code.

        Args:
            source_code: PL/SQL source text.

        Returns:
            First 16 characters of the SHA256 hex digest.
        """
        return hashlib.sha256(source_code.encode()).hexdigest()[:16]
