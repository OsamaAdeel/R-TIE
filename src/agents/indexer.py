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

# W93 — indexer validation gate
#
# Pre-W93 the description-generation LLM-failure handler returned a
# sentinel dict whose ``description`` was the literal string
# ``(indexing failed: <category>)``. The same call site then embedded
# that sentinel via OpenAI and upserted the doc with ``status="approved"``,
# producing four OFSERM docs (ENTITY_INFO_HIER_DATA_POP,
# PARTY_SHAREHOLDING_PERCENT_CALCULATION_FOR_REPORTING_ENTITY,
# FSI_STD_CAPITAL_ACCT_HEAD_POP, ABL_CAP_MITIGANT_DATA_POPULATION) that
# were unfindable via KNN — their embedding was a vector of the error
# string, uncorrelated with function semantics — yet looked healthy on
# every count-based probe. The indexer was lying about its state.
#
# The W93 gate validates each description before computing the
# embedding. Rejections write the doc with ``status="failed"`` and no
# embedding (so KNN naturally excludes it); the boot-time check in
# main.py flags any approved doc that still carries the sentinel shape.
# Same architectural pattern as the W87 unrecognized-term gate and the
# W45 empty-retrieval gate, applied at the indexer rather than at
# request time.
INDEXING_FAILED_SENTINEL_PREFIX = "(indexing failed:"
DESCRIPTION_MIN_LENGTH = 100


# W122b — source-truncation cap.
#
# Pre-W122 the cap was 3000 chars, rationalised in a comment as a
# "corporate TLS payload limit". W108's TLS-rationale verification
# (scratch/w108_tls_verification.md) confirmed that claim was stale:
# API probes at 3000 / 8000 / 12000 chars all succeed cleanly. 12000
# chars covers the body of nearly all OFSAA functions; structurally
# adversarial single-INSERT functions (MRVAR) still see only ~36% of
# source but the W122a prompt redesign + manifest ingestion compensate.
MAX_SOURCE_CHARS = 12000


# W122a — description-generation prompt.
#
# Pre-W122a the prompt had a Basel-keyword seed list ("operational
# risk, gross income, capital adequacy, …") and an exhaustive-coverage
# directive ("Include EVERY table name … EVERY significant column").
# Together they produced corpus-wide keyword-stuffing: every function
# description carried near-identical Basel framing, so semantic search
# could not distinguish similarly-named neighbors and MRVAR over-
# represented on unrelated Basel queries (B2/B4 in the P1 harness).
#
# The new prompt requires business-purpose-first opening, 3-5
# distinctive concepts that disambiguate from siblings, 3-5 distinctive
# tables/columns in prose (rest in structured fields), 150-word
# ceiling. Forbidden patterns are explicitly enumerated. The structured
# JSON output shape (tables_read / tables_written / key_columns) is
# preserved — only the description-string content rules changed —
# because the vector-store schema and the boot-time W93 gate depend on
# those fields existing.
DESCRIPTION_SYSTEM_PROMPT = """You generate one concise description of an OFSAA PL/SQL function for
semantic search retrieval. Read the function source, the manifest
description (if provided), and produce ONE description following these
rules.

REQUIRED SHAPE
- Maximum 150 words total. Often one short paragraph is enough; three
  at most.
- First sentence: state the function's SPECIFIC business purpose in
  domain-specific terms. Good openings:
    "Enforces RRP eligibility via two threshold gates on customer
     balance and risk-weight."
    "Computes operational-risk RWA under the Basic Indicator Approach."
    "Loads phase-in deductions for CET1 capital across the transition
     schedule."
- Bad openings (avoid):
    "This function populates X for a given batch..."
    "This PL/SQL function is implemented in schema Y to..."
    "The function is responsible for..."
  These waste the most important sentence on generic framing.

REQUIRED CONTENT
- Name 3-5 DISTINCTIVE concepts that disambiguate THIS function from
  similarly-named neighbors. Distinctive means: would help a reader
  pick THIS function over its siblings in the same module. Examples:
    - Specific parameter names (RTLGRAN, RTLLVE, RTLREO)
    - Threshold values (300M, 0.002, 15%)
    - CAP codes (CAP214, CAP973)
    - Specific asset classes (OTH, OTHMRB, securitization)
    - Characteristic flags (F_BIA_QUALIFIED_IND, F_LCR_BUCKET)
- If a manifest description is provided in the user message, weave its
  content into the first paragraph as ground truth. The manifest was
  written by an analyst who knows what the function is FOR.
- Name 3-5 of the MOST DISTINCTIVE tables read/written and key columns
  in prose. The structured fields (tables_read, tables_written,
  key_columns) separately hold the full lists. Do NOT repeat all of
  them in prose.

FORBIDDEN PATTERNS
- Do NOT add a closing "Keywords and concepts covered:" block. The
  structured fields exist for retrieval recall on columns/tables.
- Do NOT use meta-language about retrieval or discovery: "This
  description is intended to make the function discoverable", "for
  searches on", "Domain keywords include", "Business and regulatory
  keywords:".
- Do NOT include generic Basel keywords (capital adequacy, operational
  risk, deduction ratio, beta factor, RWA inputs) UNLESS they are
  genuinely specific to this function. If every Basel function carries
  the same framing, semantic search cannot distinguish them.
- Do NOT recite long column lists in prose. The structured fields hold
  them.

LENGTH RULE — STRICT
150 words is a hard ceiling. If you cannot describe the function in
150 words, prioritize in this order and drop the rest:
  1. The business purpose (first sentence)
  2. The 3-5 distinctive concepts
  3. The 3-5 distinctive tables/columns

OUTPUT FORMAT
Respond with ONLY valid JSON — no markdown fences, no surrounding text.

{
  "description": "<the description string following ALL the rules above>",
  "tables_read": ["TABLE1", "TABLE2"],
  "tables_written": ["TABLE3"],
  "key_columns": ["COL1", "COL2"]
}

The structured fields (tables_read, tables_written, key_columns) hold
the COMPLETE lists derived from the source — they back retrieval
recall on column / table names that the 150-word description prose
deliberately omits. Only the description string is bound by the
150-word ceiling.
"""


# W122a — post-generation validation guards.
#
# LLMs are bad at word-counting and prompt instructions are
# approximate; trust-but-verify with explicit guards. Failures trigger
# one re-prompt that includes the rejection reason; persistent failure
# after retries is logged and accepted (don't crash indexing on
# stylistic deviations — the W93 gate downstream still catches
# sentinels and too-short strings).
_W122A_FORBIDDEN_PHRASES: Tuple[str, ...] = (
    "intended to make",
    "keywords and concepts",
    "for searches on",
    "discoverable by",
    "make the function discoverable",
    "domain keywords",
    "business and regulatory keywords",
)

_W122A_GENERIC_OPENINGS: Tuple[str, ...] = (
    "this function populates",
    "this pl/sql function",
    "the function is responsible for",
    "this function is implemented in",
    "this function handles",
    "this routine",
)

# 150 word hard cap. Per the W122a design Part 6 escape hatch: if the
# 5-function gate-1 surfaces consistent fights (3+ functions trip the
# length validator), relax to 200 and re-run gate-1. If 200 still
# fights, escalate before W122d mass re-index.
_W122A_MAX_WORDS: int = 150


def _validate_description(description: str) -> Tuple[bool, Optional[str]]:
    """W122a post-generation validation. Returns ``(is_valid, reason)``.

    Distinct from :meth:`IndexerAgent._validate_description_result`
    (the W93 gate). W93 catches indexing-failure sentinels and
    description strings under :data:`DESCRIPTION_MIN_LENGTH`; W122a
    catches *quality* deviations the LLM may produce despite the
    redesigned prompt — over-length output, forbidden meta-language
    phrases, generic function-stem openings.

    Order is intentional: length-check first (cheapest), then
    forbidden-phrase scan, then opening-pattern check. Each failure
    returns a reason string used by
    :meth:`IndexerAgent._generate_description_with_validation` to
    re-prompt with a specific rejection note.
    """
    if not description:
        return False, (
            "DESCRIPTION_EMPTY: description string was empty or missing."
        )

    word_count = len(description.split())
    if word_count > _W122A_MAX_WORDS:
        return False, (
            f"DESCRIPTION_TOO_LONG: {word_count} words (limit "
            f"{_W122A_MAX_WORDS}). Prune to: business purpose, 3-5 "
            f"distinctive concepts, 3-5 distinctive tables/columns. "
            f"Drop everything else."
        )

    lower = description.lower()
    for phrase in _W122A_FORBIDDEN_PHRASES:
        if phrase in lower:
            return False, (
                f"DESCRIPTION_HAS_FORBIDDEN_PATTERN: contains "
                f"'{phrase}'. Rewrite without meta-language about "
                f"retrieval, discoverability, or keyword listings."
            )

    opening = lower[:120]
    for pattern in _W122A_GENERIC_OPENINGS:
        if pattern in opening:
            return False, (
                f"DESCRIPTION_GENERIC_OPENING: starts with '{pattern}'. "
                f"Lead with the function's SPECIFIC business purpose, "
                f"not generic boilerplate."
            )

    return True, None


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
        # W122b — raised 2000 -> 4000 after the gate-1 first run showed
        # 4/5 functions hitting LengthFinishReasonError. The W122b
        # source-cap lift (3000 -> 12000) made many more tables/columns
        # visible to the LLM; the redesigned prompt asks for COMPLETE
        # tables_read / tables_written / key_columns arrays in the JSON
        # output; for functions like MRVAR (60+ columns visible in the
        # INSERT) the structured fields alone consume most of the 2000-
        # token budget, leaving the description string truncated and
        # the JSON malformed. Raising the output budget addresses the
        # root cause directly. Same root cause as W122b (output
        # infrastructure collided with expanded source visibility).
        # Forward note: if 4000 still hits the ceiling on some
        # function, the next step is to re-examine the prompt's
        # "complete structured fields" requirement rather than raising
        # further.
        max_tokens: int = 4000,
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

        # W122c: load this module's manifest once so per-function lookup
        # of task.description is O(1) inside the loop. Modules without a
        # manifest contribute None.
        module_manifest = self._load_module_manifest(module_name)

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
            # W93: only skip when the existing doc is *approved* — a
            # ``failed`` doc has the same source_hash by construction
            # but its description never landed, so we always re-attempt.
            if not force and fn_schema:
                existing = await self._vector_store.get_function_doc(
                    fn_schema, fn_name
                )
                if (
                    existing
                    and existing.get("source_hash") == source_hash
                    and existing.get("status") == "approved"
                ):
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

                # W122b: truncate source at MAX_SOURCE_CHARS (12000).
                # Pre-W122b the cap was 3000, rationalised as a corporate
                # TLS limit that W108's verification proved stale.
                truncated_source = source_code[:MAX_SOURCE_CHARS]
                if len(source_code) > MAX_SOURCE_CHARS:
                    truncated_source += (
                        f"\n\n-- [TRUNCATED: "
                        f"{len(source_code) - MAX_SOURCE_CHARS} "
                        f"more characters]"
                    )

                # W122c: thread the analyst-written task.description
                # when the module's manifest knows about this function.
                manifest_desc = self._lookup_manifest_description(
                    module_manifest, fn_name,
                )

                print(f"    Generating description for {fn_name} ({len(source_code)} chars, sending {len(truncated_source)})...")

                # W122a: retry-on-validation-failure wrapper. Returns the
                # last attempt regardless of validation outcome — W93's
                # downstream gate still rejects sentinel / too-short.
                desc_result = await self._generate_description_with_validation(
                    fn_name, truncated_source, manifest_desc=manifest_desc,
                )

                # W93: validate before paying the embedding API call. A
                # rejected description means the LLM step failed silently
                # (sentinel) or returned something too thin to be useful;
                # mark the doc failed and move on.
                is_valid, reject_reason = self._validate_description_result(
                    desc_result
                )
                if not is_valid:
                    logger.error(
                        "W93 validation rejected %s:%s (%s); marking failed "
                        "and skipping embedding | correlation_id=%s",
                        fn_schema or "?", fn_name, reject_reason,
                        correlation_id,
                    )
                    await self._vector_store.upsert_function(
                        module=module_name,
                        function_name=fn_name,
                        description=desc_result.get("description", ""),
                        embedding=None,
                        tables_read=desc_result.get("tables_read", []),
                        tables_written=desc_result.get("tables_written", []),
                        key_columns=desc_result.get("key_columns", []),
                        source_hash=source_hash,
                        status="failed",
                        schema=fn_schema,
                        failure_reason=reject_reason,
                    )
                    errors.append({
                        "name": fn_name,
                        "error": f"description rejected: {reject_reason}",
                    })
                    continue

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
        # W122c: parallel map for manifest task descriptions.
        function_to_description = self._build_function_to_description_map()
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
                    # W93: same retry-on-failed rule as index_module — a
                    # status="failed" doc is by definition something we
                    # want to retry next pass; only "approved" docs with
                    # matching source_hash are truly up-to-date.
                    if (
                        existing
                        and existing.get("source_hash") == source_hash
                        and existing.get("status") == "approved"
                    ):
                        skipped.append(fn_name)
                        continue

                module_tag = function_to_module.get(
                    (schema, fn_name.upper()), schema
                )

                try:
                    if indexed or errors:
                        await asyncio.sleep(2)

                    # W122b: truncation cap raised 3000 -> 12000.
                    truncated_source = source_code[:MAX_SOURCE_CHARS]
                    if len(source_code) > MAX_SOURCE_CHARS:
                        truncated_source += (
                            f"\n\n-- [TRUNCATED: "
                            f"{len(source_code) - MAX_SOURCE_CHARS} "
                            f"more characters]"
                        )

                    # W122c: per-function manifest description (None
                    # when absent from the map, which is fine — the
                    # generator treats it as optional).
                    manifest_desc = function_to_description.get(
                        (schema, fn_name.upper())
                    )

                    print(
                        f"    Generating description for {fn_name} "
                        f"({len(source_code)} chars, sending "
                        f"{len(truncated_source)})..."
                    )

                    # W122a: retry-on-validation-failure wrapper.
                    desc_result = (
                        await self._generate_description_with_validation(
                            fn_name,
                            truncated_source,
                            manifest_desc=manifest_desc,
                        )
                    )

                    # W93: validate before embedding. See index_module
                    # for rationale.
                    is_valid, reject_reason = self._validate_description_result(
                        desc_result
                    )
                    if not is_valid:
                        logger.error(
                            "W93 validation rejected %s:%s (%s); marking "
                            "failed and skipping embedding | "
                            "correlation_id=%s",
                            schema, fn_name, reject_reason, correlation_id,
                        )
                        await self._vector_store.upsert_function(
                            module=module_tag,
                            function_name=fn_name,
                            description=desc_result.get("description", ""),
                            embedding=None,
                            tables_read=desc_result.get("tables_read", []),
                            tables_written=desc_result.get(
                                "tables_written", []
                            ),
                            key_columns=desc_result.get("key_columns", []),
                            source_hash=source_hash,
                            status="failed",
                            schema=schema,
                            failure_reason=reject_reason,
                        )
                        errors.append({
                            "name": fn_name,
                            "error": f"description rejected: {reject_reason}",
                        })
                        continue

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

    def _load_module_manifest(self, module_name: str):
        """W122c: load the manifest for a single module directory.

        Returns the :class:`BatchManifest` for the module folder whose
        basename matches ``module_name`` (case-insensitive), or ``None``
        when no such folder exists or no manifest.yaml is present.
        Failures bubble up as ``None`` rather than raising — the
        indexer must keep running even on a malformed manifest, since
        a missing description is non-fatal.
        """
        for modules_dir in MODULES_DIRS:
            if not os.path.isdir(modules_dir):
                continue
            for entry in os.listdir(modules_dir):
                module_path = os.path.join(modules_dir, entry)
                if not os.path.isdir(module_path):
                    continue
                if (
                    entry.upper() == module_name.upper()
                    or module_name.upper() in entry.upper()
                ):
                    try:
                        return load_manifest(module_path)
                    except Exception as exc:
                        logger.debug(
                            "manifest load failed for %s: %s",
                            module_path, exc,
                        )
                        return None
        return None

    @staticmethod
    def _lookup_manifest_description(
        manifest, function_name: str,
    ) -> Optional[str]:
        """W122c: return ``task.description`` for ``function_name`` or None."""
        if manifest is None:
            return None
        task = manifest.get_task(function_name)
        if task is None:
            return None
        desc = (task.description or "").strip()
        return desc or None

    def _build_function_to_description_map(
        self,
    ) -> Dict[Tuple[str, str], str]:
        """W122c: return ``(schema, FN_UPPER) -> manifest task description``.

        Mirrors :meth:`_build_function_to_module_map` but pulls
        ``task.description`` rather than ``manifest.batch``. Tasks
        without a description are absent from the map (the indexer
        threads the value only when present).
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
                    desc = (task.description or "").strip()
                    if not desc:
                        continue
                    fn_upper = task.name.strip().upper()
                    mapping[(schema, fn_upper)] = desc
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
        self,
        function_name: str,
        source_code: str,
        manifest_desc: Optional[str] = None,
        retry_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate a rich description of a PL/SQL function via LLM.

        Args:
            function_name: Name of the function.
            source_code: Full PL/SQL source code (already truncated to
                :data:`MAX_SOURCE_CHARS` upstream).
            manifest_desc: Analyst-written description from the module's
                manifest.yaml (W122c). When present it's woven into the
                user message as ground truth — the analyst knows what
                the function is FOR, which the LLM cannot always
                derive from severely-truncated source alone.
            retry_reason: When the previous attempt failed W122a
                post-gen validation, the rejection reason is fed back
                in so the next attempt has specific guidance. None on
                the first attempt.

        Returns:
            Dict with description, tables_read, tables_written, key_columns.
        """
        # Use OpenAI for indexing (one-time, fast).
        # W34c: site-default is gpt-4o-mini (SITE_MODEL_DEFAULTS).
        # OPENAI_MODEL env, when set, still wins (explicit-model arg
        # > site default), preserving the prior override semantic.
        llm = create_llm(
            provider="openai",
            model=os.getenv("OPENAI_MODEL"),
            site="indexer.generate_description",
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            json_mode=True,
        )

        # W122c: thread the manifest description in when available.
        # The structured user-message pieces are concatenated rather
        # than embedded inline so the LLM can attend to each block
        # separately.
        user_parts: List[str] = [
            "Generate a semantic search description for this PL/SQL function.",
            f"FUNCTION NAME: {function_name}",
        ]
        if manifest_desc:
            user_parts.append(
                f"MANIFEST DESCRIPTION (analyst-written, treat as "
                f"ground truth for what the function is FOR): "
                f"{manifest_desc}"
            )
        user_parts.append(
            f"FUNCTION SOURCE (may be truncated):\n{source_code}"
        )
        if retry_reason:
            user_parts.append(
                f"PREVIOUS ATTEMPT WAS REJECTED FOR: {retry_reason}\n"
                f"Rewrite respecting the rules."
            )
        messages = [
            SystemMessage(content=DESCRIPTION_SYSTEM_PROMPT),
            HumanMessage(content="\n\n".join(user_parts)),
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

    async def _generate_description_with_validation(
        self,
        function_name: str,
        source_code: str,
        manifest_desc: Optional[str] = None,
        max_retries: int = 2,
    ) -> Dict[str, Any]:
        """W122a: generate a description with post-gen quality guards.

        Calls :meth:`_generate_description` and validates the returned
        dict's ``description`` field against the W122a rules (length,
        forbidden phrases, generic opening). On failure, re-prompts up
        to ``max_retries`` times with the rejection reason fed back in.

        After retries are exhausted, the latest attempt is returned
        anyway with an error-level log line — indexing should not crash
        on stylistic deviations, and the W93 gate downstream still
        catches sentinel / too-short failures.

        Indexing-failure sentinels (``"(indexing failed: …)"``) are
        passed through without retry because they signal an LLM-call
        exception, not a quality issue; re-prompting under the same
        conditions would just burn API budget. W93's
        :meth:`_validate_description_result` catches them downstream.
        """
        desc_result = await self._generate_description(
            function_name, source_code, manifest_desc=manifest_desc,
        )

        for attempt in range(max_retries):
            description = (desc_result.get("description") or "").strip()
            if description.startswith(INDEXING_FAILED_SENTINEL_PREFIX):
                return desc_result

            is_valid, reason = _validate_description(description)
            if is_valid:
                return desc_result

            logger.warning(
                "W122a validation failed for %s (attempt %d/%d): %s. "
                "Retrying.",
                function_name, attempt + 1, max_retries, reason,
            )
            desc_result = await self._generate_description(
                function_name,
                source_code,
                manifest_desc=manifest_desc,
                retry_reason=reason,
            )

        final_description = (desc_result.get("description") or "").strip()
        if not final_description.startswith(INDEXING_FAILED_SENTINEL_PREFIX):
            is_valid, reason = _validate_description(final_description)
            if not is_valid:
                logger.error(
                    "W122a validation persistently failed for %s after "
                    "%d retries: %s. Accepting output anyway (W93 still "
                    "gates sentinel / too-short).",
                    function_name, max_retries, reason,
                )
        return desc_result

    @staticmethod
    def _validate_description_result(
        desc_result: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """W93 validation gate. Returns ``(is_valid, reason)``.

        Rejects descriptions that would make a doc unretrievable while
        still being marked approved. The two rejection categories caught
        today:

        * ``sentinel_prefix`` — the LLM-failure handler at
          :meth:`_generate_description` returns ``(indexing failed: …)``.
          Embedding this string produces a vector uncorrelated with
          function semantics.
        * ``too_short`` — anything under
          :data:`DESCRIPTION_MIN_LENGTH` characters. The floor is set
          well below the 500-char real-but-stunted bucket (47 OFSERM
          functions live there) but well above the 42-char sentinel,
          so legitimate-if-thin descriptions are still accepted while
          future failure shapes are caught.

        Order matters: ``sentinel_prefix`` is checked first because the
        sentinel is also under the length floor — checking it explicitly
        gives operators a specific reason rather than a generic
        "too_short".
        """
        description = (desc_result.get("description") or "").strip()
        if description.startswith(INDEXING_FAILED_SENTINEL_PREFIX):
            return False, "sentinel_prefix"
        if len(description) < DESCRIPTION_MIN_LENGTH:
            return False, "too_short"
        return True, ""

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
