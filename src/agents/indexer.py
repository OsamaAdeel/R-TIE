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
from typing import Any, Dict, List, Optional

from langchain_openai import OpenAIEmbeddings
from langchain_core.messages import SystemMessage, HumanMessage

from src.tools.vector_store import VectorStore
from src.llm_factory import create_llm
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id

logger = get_logger(__name__, concern="app")

# Same module paths as metadata_interpreter
_RTIE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
MODULES_DIRS = [
    os.path.join(_RTIE_ROOT, "db", "modules"),
    os.path.join(os.path.dirname(_RTIE_ROOT), "db", "modules"),
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

            # Check if already indexed with same source
            if not force:
                existing = await self._vector_store.get_function_doc(
                    module_name, fn_name
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

                # Generate description via LLM
                desc_result = await self._generate_description(fn_name, source_code)

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
        # Use Ollama for description generation (large source payloads)
        llm = create_llm(
            provider="ollama",
            model=os.getenv("OLLAMA_MODEL", "llama3:8b"),
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            json_mode=self._llm_provider != "anthropic",
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

        response = await llm.ainvoke(messages)
        raw = response.content.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        return json.loads(raw)

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
