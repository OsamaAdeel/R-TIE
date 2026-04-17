"""
RTIE Logic Explainer Agent.

Uses an LLM (OpenAI or Claude) to generate structured, fully-cited
explanations of PL/SQL functions and procedures. Every claim in the
explanation must reference specific line numbers from the source code.
LangSmith tracing is enabled on all LLM calls. Supports dynamic model
switching per request.
"""

import json
from typing import Any, Dict, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage

from src.pipeline.state import LogicState
from src.llm_factory import create_llm
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id

logger = get_logger(__name__, concern="app")


EXPLANATION_SYSTEM_PROMPT = """You are an expert PL/SQL analyst for the RTIE system (Regulatory Trace & Intelligence Engine).
You analyze Oracle OFSAA PL/SQL functions and procedures used in regulatory capital computations.

You will receive:
1. The complete source code of a PL/SQL function/procedure with line numbers.
2. A call tree showing all dependencies and their source code.

Your task is to produce a structured JSON explanation. You MUST respond with ONLY valid JSON — no markdown, no extra text.

STRICT RULES:
- ONLY reference line numbers and content that exist in the provided source code.
- NEVER hallucinate logic, functions, or formulas that are not in the source.
- Cite specific line numbers for EVERY claim you make.
- If something is unclear or ambiguous, FLAG IT rather than guessing.
- Every formula must map to exact lines in the source code.
- Every dependency mentioned must exist in the call tree provided.

Output JSON schema:
{
  "summary": "A concise plain-English summary of what the function/procedure does",
  "step_by_step": [
    {
      "step": 1,
      "description": "What this step does",
      "lines": [10, 11, 12],
      "code_snippet": "relevant code from those lines"
    }
  ],
  "formulas": [
    {
      "name": "Formula name or description",
      "formula": "The mathematical formula",
      "lines": [15, 16],
      "variables": {"var_name": "description of what it represents"}
    }
  ],
  "dependencies_used": [
    {
      "name": "FN_DEPENDENCY_NAME",
      "purpose": "What this dependency does in context",
      "called_at_lines": [25, 30]
    }
  ],
  "regulatory_refs": [
    "Any regulatory framework references found (Basel III, IFRS 9, etc.)"
  ],
  "raw_source_references": [
    {
      "line": 10,
      "text": "exact text from that line",
      "significance": "why this line matters"
    }
  ],
  "unclear_items": [
    "Anything that could not be determined from the source code alone"
  ]
}
"""


SEMANTIC_EXPLANATION_PROMPT = """You are an expert in Oracle OFSAA FSAPPS regulatory capital calculations.
You receive source code from one or more PL/SQL functions and must explain the BUSINESS MEANING and DATA FLOW — not the syntax.

RULES:
1. Never explain what SQL syntax does (do not explain NVL, CASE, TO_NUMBER, DECODE).
   Instead explain what the VALUE represents and why it changes.

2. For every step, answer these questions:
   - What is the value at this point?
   - Where did it come from (which table, which column)?
   - Why is it being changed?
   - What does the result mean in business terms?

3. For intermediate variables (local PL/SQL variables like TOT1, CBA_DEDUCTION):
   - Explain the formula in plain English
   - Name the source tables and what data they contribute
   - Show the arithmetic clearly: e.g. "DBS GL balance × deduction ratio"

4. Always include execution conditions prominently:
   "This entire function ONLY runs when the reporting month is December."
   Never bury this at the end — state it first for the function.

5. For steps where a value is copied unchanged between tables:
   State clearly: "The value is passed through without modification."

6. Cite every claim with function name and line numbers.

7. End with a SHORT SUMMARY (4 sentences max) that states:
   - Where the value originates
   - What transforms it
   - What the final value represents
   - Any important conditions (e.g. December-only)

FORMAT:
- Use ## for main heading, ### for each function/step
- Include ```sql code blocks with the relevant PL/SQL
- Put line references in section headers: ### Step 1: Initial Insert (Lines 203-223)
- Do NOT repeat line references separately below code blocks
"""


class LogicExplainer:
    """Agent for generating structured PL/SQL logic explanations.

    Uses OpenAI or Anthropic LLMs with LangSmith tracing to analyze
    source code and produce fully-cited, step-by-step explanations of
    regulatory capital computation logic. Model is selectable per request.
    """

    def __init__(
        self,
        temperature: float = 0,
        max_tokens: int = 2000,
        langsmith_project: str = "RTIE",
    ) -> None:
        """Initialize the LogicExplainer with LLM settings.

        Args:
            temperature: LLM temperature. Defaults to 0.
            max_tokens: Maximum tokens for LLM response. Defaults to 2000.
            langsmith_project: LangSmith project name for tracing. Defaults to 'RTIE'.
        """
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._langsmith_project = langsmith_project

    def _get_llm(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> BaseChatModel:
        """Get an LLM instance for the specified provider.

        Args:
            provider: 'openai' or 'anthropic'. None uses default.
            model: Specific model name. None uses default for provider.

        Returns:
            A LangChain chat model instance.
        """
        return create_llm(
            provider=provider,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            json_mode=(provider or "openai") != "anthropic",
        )

    async def explain_logic(
        self,
        state: LogicState,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LogicState:
        """Generate a structured explanation of the PL/SQL source code.

        Sends the full source code and call tree to the selected LLM,
        which returns a structured JSON explanation with line citations.
        LangSmith tracing is active for this call.

        Args:
            state: Current pipeline state with source_code and call_tree.
            provider: LLM provider ('openai' or 'anthropic'). None uses default.
            model: Specific model name. None uses default for provider.

        Returns:
            Updated state with explanation dict populated.
        """
        correlation_id = get_correlation_id()
        object_name = state["object_name"]
        schema = state["schema"]

        logger.info(
            f"Generating explanation for {schema}.{object_name} | "
            f"provider={provider}, model={model} | "
            f"correlation_id={correlation_id}"
        )

        llm = self._get_llm(provider, model)

        # Format source code for the LLM
        source_text = self._format_source_code(state["source_code"])
        call_tree_text = self._format_call_tree(state["call_tree"])

        system_prompt = EXPLANATION_SYSTEM_PROMPT
        if (provider or "").lower() == "anthropic":
            system_prompt += (
                "\n\nIMPORTANT: Respond with ONLY the raw JSON object. "
                "No markdown code fences, no explanation before or after."
            )

        user_prompt = (
            f"Analyze the following PL/SQL object: {schema}.{object_name}\n\n"
            f"=== SOURCE CODE ===\n{source_text}\n\n"
            f"=== CALL TREE (Dependencies) ===\n{call_tree_text}\n\n"
            f"Produce a complete structured JSON explanation following the "
            f"schema in your instructions. Cite every claim with line numbers."
        )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]

        response = await llm.ainvoke(messages)

        raw_content = response.content.strip()

        # Strip markdown fences if present (Claude sometimes adds them)
        if raw_content.startswith("```"):
            raw_content = raw_content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        logger.info(
            f"LLM explanation received for {schema}.{object_name} "
            f"({len(raw_content)} chars) | correlation_id={correlation_id}"
        )

        # Parse the JSON response
        explanation = json.loads(raw_content)

        state["explanation"] = explanation

        logger.info(
            f"Explanation parsed: {len(explanation.get('step_by_step', []))} steps, "
            f"{len(explanation.get('formulas', []))} formulas, "
            f"{len(explanation.get('dependencies_used', []))} deps | "
            f"correlation_id={correlation_id}"
        )
        return state

    async def explain_semantic(
        self,
        state: LogicState,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LogicState:
        """Generate explanation across multiple functions found via semantic search.

        Receives all relevant function sources and the user's original question,
        then produces a unified cross-function explanation with citations.

        Args:
            state: Pipeline state with raw_query and multi_source.
            provider: LLM provider. None uses default.
            model: Model name. None uses default.

        Returns:
            Updated state with explanation dict.
        """
        correlation_id = get_correlation_id()
        query = state["raw_query"]
        multi_source = state.get("multi_source", {})

        logger.info(
            f"Generating semantic explanation for: {query[:80]}... "
            f"({len(multi_source)} functions) | "
            f"provider={provider}, model={model} | "
            f"correlation_id={correlation_id}"
        )

        llm = self._get_llm(provider, model)

        # Check if graph pipeline produced a structured payload
        llm_payload = state.get("llm_payload")
        if llm_payload and state.get("graph_available"):
            logger.info("explain_semantic: using graph pipeline payload (%d chars)", len(llm_payload))
            user_prompt = (
                f"User Question: {query}\n\n"
                f"The following structured analysis was produced from the parsed PL/SQL graph:\n\n"
                f"{llm_payload}\n\n"
                "Answer the user's question with a detailed markdown explanation. "
                "Cite specific function names and line numbers for every claim."
            )
        else:
            logger.info("explain_semantic: falling back to raw source")
            # Format all function sources
            function_sections = []
            for fn_name, fn_data in multi_source.items():
                source_text = self._format_source_code(fn_data.get("source_code", []))
                section = (
                    f"=== FUNCTION: {fn_name} (relevance: {fn_data.get('score', 0):.4f}) ===\n"
                    f"Description: {fn_data.get('description', 'N/A')}\n"
                    f"Tables Read: {fn_data.get('tables_read', 'N/A')}\n"
                    f"Tables Written: {fn_data.get('tables_written', 'N/A')}\n\n"
                    f"Source Code:\n{source_text}\n"
                )
                function_sections.append(section)

            user_prompt = (
                f"User Question: {query}\n\n"
                f"The following {len(multi_source)} functions were found via semantic search:\n\n"
                + "\n".join(function_sections)
                + "\n\nAnswer the user's question with a detailed markdown explanation. "
                "Cite specific function names and line numbers for every claim."
            )

        # Use non-JSON mode for markdown responses
        llm = create_llm(
            provider=provider,
            model=model,
            temperature=self._temperature,
            max_tokens=4096,
            json_mode=False,
        )

        messages = [
            SystemMessage(content=SEMANTIC_EXPLANATION_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        response = await llm.ainvoke(messages)
        markdown_content = response.content.strip()

        # Store as markdown explanation
        state["explanation"] = {
            "markdown": markdown_content,
            "summary": markdown_content[:200] + "..." if len(markdown_content) > 200 else markdown_content,
        }

        logger.info(
            f"Semantic explanation generated: "
            f"{len(markdown_content)} chars markdown | "
            f"correlation_id={correlation_id}"
        )
        return state

    async def stream_semantic(
        self,
        state: LogicState,
        provider: str | None = None,
        model: str | None = None,
    ):
        """Stream semantic explanation tokens as an async generator.

        Yields markdown tokens one chunk at a time for SSE streaming.
        Does NOT update state — the caller collects the full text.

        Args:
            state: Pipeline state with raw_query and multi_source.
            provider: LLM provider.
            model: Model name.

        Yields:
            String chunks of the markdown response.
        """
        query = state["raw_query"]

        # Check if graph pipeline produced a structured payload
        llm_payload = state.get("llm_payload")
        if llm_payload and state.get("graph_available"):
            logger.info("stream_semantic: using graph pipeline payload (%d chars)", len(llm_payload))
            user_prompt = (
                f"User Question: {query}\n\n"
                f"The following structured analysis was produced from the parsed PL/SQL graph:\n\n"
                f"{llm_payload}\n\n"
                "Answer the user's question with a detailed markdown explanation. "
                "Cite specific function names and line numbers for every claim."
            )
        else:
            logger.info("stream_semantic: falling back to raw source")
            multi_source = state.get("multi_source", {})
            function_sections = []
            for fn_name, fn_data in multi_source.items():
                source_text = self._format_source_code(fn_data.get("source_code", []))
                section = (
                    f"=== FUNCTION: {fn_name} (relevance: {fn_data.get('score', 0):.4f}) ===\n"
                    f"Description: {fn_data.get('description', 'N/A')}\n"
                    f"Tables Read: {fn_data.get('tables_read', 'N/A')}\n"
                    f"Tables Written: {fn_data.get('tables_written', 'N/A')}\n\n"
                    f"Source Code:\n{source_text}\n"
                )
                function_sections.append(section)

            user_prompt = (
                f"User Question: {query}\n\n"
                f"The following {len(multi_source)} functions were found via semantic search:\n\n"
                + "\n".join(function_sections)
                + "\n\nAnswer the user's question with a detailed markdown explanation. "
                "Cite specific function names and line numbers for every claim."
            )

        llm = create_llm(
            provider=provider,
            model=model,
            temperature=self._temperature,
            max_tokens=4096,
            json_mode=False,
        )

        messages = [
            SystemMessage(content=SEMANTIC_EXPLANATION_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        async for chunk in llm.astream(messages):
            if chunk.content:
                yield chunk.content

    def _format_source_code(self, source_lines: list) -> str:
        """Format source code lines for LLM consumption.

        Args:
            source_lines: List of dicts with 'line' and 'text' keys,
                or raw strings.

        Returns:
            Formatted string with line numbers and code text.
        """
        lines = []
        for item in source_lines:
            if isinstance(item, dict):
                line_num = item.get("line", "?")
                text = item.get("text", "").rstrip("\n")
                lines.append(f"L{line_num}: {text}")
            else:
                lines.append(str(item))
        return "\n".join(lines)

    def _format_call_tree(self, call_tree: dict) -> str:
        """Format the call tree for LLM consumption.

        Args:
            call_tree: Nested dependency dictionary.

        Returns:
            Human-readable string representation of the call tree.
        """
        return json.dumps(call_tree, indent=2, default=str)
