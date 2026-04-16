"""
RTIE Validator Agent.

Contains three validators that ensure cache freshness, query relevance,
and output accuracy:
- cache_validator: checks Oracle DDL timestamps against cached versions
- query_relevance_validator: pure Python check for object name in response
- output_validator: verifies all referenced functions exist in call tree
"""

from typing import Any, Dict, List, Set

from src.pipeline.state import LogicState
from src.tools.schema_tools import SchemaTools
from src.tools.cache_tools import CacheClient
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id

logger = get_logger(__name__, concern="validator")


class Validator:
    """Multi-purpose validation agent for the RTIE pipeline.

    Performs three independent validations on the pipeline state:
    cache freshness, query relevance, and output reference integrity.
    """

    def __init__(
        self,
        schema_tools: SchemaTools,
        cache_client: CacheClient,
    ) -> None:
        """Initialize the Validator with Oracle and cache clients.

        Args:
            schema_tools: Oracle query execution tools for DDL checks.
            cache_client: Redis cache client for cached version retrieval.
        """
        self._schema_tools = schema_tools
        self._cache = cache_client

    async def cache_validator(self, state: LogicState) -> LogicState:
        """Validate cache freshness by comparing Oracle DDL timestamps.

        Checks the oracle_last_ddl_time stored in the cached object against
        the current LAST_DDL_TIME in Oracle's ALL_OBJECTS. If they differ,
        sets cache_stale=True and logs a WARNING.

        Args:
            state: Current pipeline state with cache info.

        Returns:
            Updated state with cache_stale flag set.
        """
        correlation_id = get_correlation_id()
        schema = state["schema"]
        object_name = state["object_name"]

        logger.info(
            f"Validating cache freshness for {schema}.{object_name} | "
            f"correlation_id={correlation_id}"
        )

        # If not a cache hit, nothing to validate
        if not state.get("cache_hit", False):
            state["cache_stale"] = False
            logger.info(
                f"Skipping cache validation — not a cache hit | "
                f"correlation_id={correlation_id}"
            )
            return state

        # Get current DDL time from Oracle
        rows = await self._schema_tools.execute_query(
            "TMPL_OBJECT_EXISTS",
            {"schema": schema, "object_name": object_name},
        )

        if not rows:
            state["cache_stale"] = True
            logger.warning(
                f"Object {schema}.{object_name} no longer exists in Oracle — "
                f"cache is stale | correlation_id={correlation_id}"
            )
            return state

        oracle_ddl_time = str(rows[0][2])

        # Get cached DDL time from Redis
        cached = await self._cache.get_json("logic", schema, object_name)
        cached_ddl_time = cached.get("oracle_last_ddl_time") if cached else None

        if cached_ddl_time != oracle_ddl_time:
            state["cache_stale"] = True
            logger.warning(
                f"Cache STALE for {schema}.{object_name}: "
                f"cached_ddl={cached_ddl_time} vs oracle_ddl={oracle_ddl_time} | "
                f"correlation_id={correlation_id}"
            )
        else:
            state["cache_stale"] = False
            logger.info(
                f"Cache is FRESH for {schema}.{object_name} | "
                f"correlation_id={correlation_id}"
            )

        return state

    async def query_relevance_validator(self, state: LogicState) -> LogicState:
        """Validate that the explanation references the queried object.

        Pure Python validation — no LLM calls. Checks that the object_name
        appears in the explanation text and that key query terms are present
        in the response.

        Args:
            state: Current pipeline state with explanation and raw_query.

        Returns:
            Updated state with relevance warnings appended if issues found.
        """
        correlation_id = get_correlation_id()
        object_name = state["object_name"]
        raw_query = state["raw_query"]
        explanation = state.get("explanation", {})
        warnings = list(state.get("warnings", []))

        logger.info(
            f"Validating query relevance for {object_name} | "
            f"correlation_id={correlation_id}"
        )

        # Flatten explanation to searchable text
        explanation_text = self._flatten_explanation(explanation)
        explanation_upper = explanation_text.upper()

        # Check 1: object_name must appear in explanation
        if object_name.upper() not in explanation_upper:
            warning = (
                f"RELEVANCE: Object name '{object_name}' not found in "
                f"explanation text"
            )
            warnings.append(warning)
            logger.warning(f"{warning} | correlation_id={correlation_id}")

        # Check 2: key query terms should appear in explanation
        query_terms = self._extract_key_terms(raw_query)
        missing_terms = [
            term for term in query_terms
            if term.upper() not in explanation_upper
        ]
        if missing_terms:
            warning = (
                f"RELEVANCE: Query terms not found in explanation: "
                f"{missing_terms}"
            )
            warnings.append(warning)
            logger.warning(f"{warning} | correlation_id={correlation_id}")

        state["warnings"] = warnings

        logger.info(
            f"Query relevance validation complete: "
            f"{len(warnings)} warnings | correlation_id={correlation_id}"
        )
        return state

    async def output_validator(self, state: LogicState) -> LogicState:
        """Validate that all referenced functions exist in the call tree.

        Checks every function name mentioned in the explanation's
        dependencies_used against the call_tree. Computes a confidence
        score as resolved_references / total_references.

        Args:
            state: Current pipeline state with explanation and call_tree.

        Returns:
            Updated state with validated flag, confidence score, and
            any validation warnings.
        """
        correlation_id = get_correlation_id()
        explanation = state.get("explanation", {})
        call_tree = state.get("call_tree", {})
        warnings = list(state.get("warnings", []))

        logger.info(
            f"Validating output references | correlation_id={correlation_id}"
        )

        # Extract all function names from explanation
        mentioned_deps = self._extract_mentioned_functions(explanation)

        # Extract all resolved function names from call tree
        resolved_names = self._extract_call_tree_names(call_tree)

        total_refs = len(mentioned_deps)
        resolved_refs = 0

        for dep_name in mentioned_deps:
            if dep_name.upper() in resolved_names:
                resolved_refs += 1
            else:
                warning = (
                    f"OUTPUT: Function '{dep_name}' mentioned in explanation "
                    f"but not found in call tree"
                )
                warnings.append(warning)
                logger.warning(f"{warning} | correlation_id={correlation_id}")

        # Compute confidence
        confidence = resolved_refs / total_refs if total_refs > 0 else 1.0
        validated = len([w for w in warnings if w.startswith("OUTPUT:")]) == 0

        state["validated"] = validated
        state["confidence"] = round(confidence, 4)
        state["warnings"] = warnings

        logger.info(
            f"Output validation complete: validated={validated}, "
            f"confidence={confidence:.4f}, "
            f"resolved={resolved_refs}/{total_refs} | "
            f"correlation_id={correlation_id}"
        )
        return state

    def _flatten_explanation(self, explanation: dict) -> str:
        """Flatten an explanation dict into a searchable text string.

        Args:
            explanation: The structured explanation dictionary.

        Returns:
            Concatenated string of all text values in the explanation.
        """
        parts = []

        if "summary" in explanation:
            parts.append(str(explanation["summary"]))

        for step in explanation.get("step_by_step", []):
            parts.append(str(step.get("description", "")))
            parts.append(str(step.get("code_snippet", "")))

        for formula in explanation.get("formulas", []):
            parts.append(str(formula.get("name", "")))
            parts.append(str(formula.get("formula", "")))

        for dep in explanation.get("dependencies_used", []):
            parts.append(str(dep.get("name", "")))
            parts.append(str(dep.get("purpose", "")))

        for ref in explanation.get("regulatory_refs", []):
            parts.append(str(ref))

        for src_ref in explanation.get("raw_source_references", []):
            parts.append(str(src_ref.get("text", "")))
            parts.append(str(src_ref.get("significance", "")))

        return " ".join(parts)

    def _extract_key_terms(self, query: str) -> List[str]:
        """Extract significant terms from a query string.

        Filters out common stop words and short tokens to identify
        the key terms that should appear in a relevant explanation.

        Args:
            query: The raw user query string.

        Returns:
            List of significant terms from the query.
        """
        stop_words = {
            "what", "does", "how", "the", "is", "a", "an", "of", "in",
            "for", "to", "and", "or", "it", "this", "that", "do", "can",
            "explain", "show", "me", "please", "logic", "behind",
        }
        words = query.split()
        return [
            w for w in words
            if len(w) > 2 and w.lower() not in stop_words
        ]

    def _extract_mentioned_functions(self, explanation: dict) -> List[str]:
        """Extract function names mentioned in the explanation.

        Args:
            explanation: The structured explanation dictionary.

        Returns:
            List of dependency function names found in the explanation.
        """
        deps = []
        for dep in explanation.get("dependencies_used", []):
            name = dep.get("name", "")
            if name:
                deps.append(name)
        return deps

    def _extract_call_tree_names(self, call_tree: dict) -> Set[str]:
        """Recursively extract all function names from the call tree.

        Args:
            call_tree: The dependency call tree dictionary.

        Returns:
            Set of uppercase function names found in the tree.
        """
        names: Set[str] = set()

        deps = call_tree.get("dependencies", {})
        if isinstance(deps, dict):
            for name, info in deps.items():
                names.add(name.upper())
                if isinstance(info, dict) and "dependencies" in info:
                    sub_names = self._extract_call_tree_names(
                        {"dependencies": info["dependencies"]}
                    )
                    names.update(sub_names)

        return names
