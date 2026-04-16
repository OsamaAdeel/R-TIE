"""
RTIE Renderer Agent.

Structures the final output response from the pipeline state, including
the explanation, confidence score, warnings, source citations, cache
status, and traceability identifiers.
"""

from typing import Any, Dict

from src.pipeline.state import LogicState
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id

logger = get_logger(__name__, concern="app")


class Renderer:
    """Agent for rendering the final structured response.

    Takes the fully populated pipeline state and assembles a clean,
    structured output dictionary suitable for API responses.
    """

    async def render_response(self, state: LogicState) -> LogicState:
        """Render the final output from the pipeline state.

        Assembles the structured response including explanation, confidence,
        warnings, source citations, metadata, and an UNVERIFIED badge if
        validation failed.

        Args:
            state: Fully populated pipeline state after all agents have run.

        Returns:
            Updated state with output dict populated.
        """
        correlation_id = get_correlation_id()

        logger.info(
            f"Rendering response for {state.get('object_name', 'unknown')} | "
            f"correlation_id={correlation_id}"
        )

        explanation = state.get("explanation", {})
        validated = state.get("validated", False)
        confidence = state.get("confidence", 0.0)
        warnings = state.get("warnings", [])

        # Build source citations from explanation references
        source_citations = self._extract_citations(explanation)

        # Build the output dict
        output: Dict[str, Any] = {
            "object_name": state.get("object_name", ""),
            "object_type": state.get("object_type", ""),
            "schema": state.get("schema", ""),
            "explanation": explanation,
            "confidence": confidence,
            "validated": validated,
            "warnings": warnings,
            "source_citations": source_citations,
            "cache_hit": state.get("cache_hit", False),
            "cache_stale": state.get("cache_stale", False),
            "session_id": state.get("session_id", ""),
            "correlation_id": state.get("correlation_id", correlation_id),
        }

        # Add semantic search metadata
        search_results = state.get("search_results", [])
        if search_results:
            output["search_results"] = [
                {"function_name": r["function_name"], "score": r.get("score", 0)}
                for r in search_results
            ]
            output["functions_analyzed"] = list(
                state.get("multi_source", {}).keys()
            )

        # Add UNVERIFIED badge if validation failed
        if not validated:
            output["badge"] = "UNVERIFIED"
            logger.warning(
                f"Response marked as UNVERIFIED for "
                f"{state.get('object_name', 'unknown')} | "
                f"correlation_id={correlation_id}"
            )
        else:
            output["badge"] = "VERIFIED"

        state["output"] = output

        logger.info(
            f"Response rendered: object={output['object_name']}, "
            f"confidence={confidence}, validated={validated}, "
            f"badge={output['badge']}, "
            f"citations={len(source_citations)} | "
            f"correlation_id={correlation_id}"
        )
        return state

    def _extract_citations(self, explanation: dict) -> list:
        """Extract source code citations from the explanation.

        Collects line references from raw_source_references, step_by_step,
        and formulas sections of the explanation.

        Args:
            explanation: The structured explanation dictionary.

        Returns:
            List of citation dicts with line, text, and context.
        """
        citations = []

        # From raw_source_references
        for ref in explanation.get("raw_source_references", []):
            citations.append({
                "line": ref.get("line"),
                "text": ref.get("text", ""),
                "context": ref.get("significance", ""),
                "source": "raw_reference",
            })

        # From step_by_step
        for step in explanation.get("step_by_step", []):
            for line_num in step.get("lines", []):
                citations.append({
                    "line": line_num,
                    "text": step.get("code_snippet", ""),
                    "context": step.get("description", ""),
                    "source": f"step_{step.get('step', '?')}",
                })

        # From formulas
        for formula in explanation.get("formulas", []):
            for line_num in formula.get("lines", []):
                citations.append({
                    "line": line_num,
                    "text": formula.get("formula", ""),
                    "context": formula.get("name", ""),
                    "source": "formula",
                })

        # De-duplicate by line number
        seen_lines = set()
        unique_citations = []
        for citation in citations:
            line = citation.get("line")
            if line is not None and line not in seen_lines:
                seen_lines.add(line)
                unique_citations.append(citation)

        return unique_citations
