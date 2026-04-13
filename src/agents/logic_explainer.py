"""
RTIE Logic Explainer Agent.

Uses an LLM (OpenAI or Claude) to generate structured, fully-cited
explanations of PL/SQL functions and procedures. Every claim in the
explanation must reference specific line numbers from the source code.
LangSmith tracing is enabled on all LLM calls. Supports dynamic model
switching per request.
"""

import asyncio
import json
from typing import Any, Dict, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage

from src.graph.state import LogicState
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


SEMANTIC_EXPLANATION_PROMPT = """You are an expert PL/SQL analyst for the RTIE system (Regulatory Trace & Intelligence Engine).
You are answering a user's question by analyzing MULTIPLE PL/SQL functions found via semantic search.

You will receive:
1. The user's original question.
2. Multiple functions with their source code, descriptions, and relevance scores.

Your task is to answer the user's question by explaining HOW the relevant functions work together.

STRICT RULES:
- ONLY reference code that exists in the provided sources.
- Cite specific function names and line numbers for EVERY claim.
- If a function is not relevant to the question, say so briefly and focus on the relevant ones.
- Explain the data flow across functions if applicable.
- Never hallucinate logic, functions, or formulas not in the source.

You MUST respond with ONLY valid JSON — no markdown, no extra text.

Output JSON schema:
{
  "summary": "Direct answer to the user's question in plain English",
  "relevant_functions": [
    {
      "name": "FUNCTION_NAME",
      "relevance": "Why this function is relevant to the question",
      "key_logic": [
        {
          "step": 1,
          "description": "What this part does relevant to the question",
          "lines": [10, 11],
          "code_snippet": "relevant code"
        }
      ]
    }
  ],
  "data_flow": "How data flows across the relevant functions (if applicable)",
  "step_by_step": [
    {
      "step": 1,
      "description": "Overall step description across functions",
      "function": "FUNCTION_NAME",
      "lines": [10, 11],
      "code_snippet": "relevant code"
    }
  ],
  "formulas": [
    {
      "name": "Formula description",
      "formula": "The calculation",
      "function": "FUNCTION_NAME",
      "lines": [15, 16],
      "variables": {"var_name": "description"}
    }
  ],
  "dependencies_used": [],
  "regulatory_refs": [],
  "raw_source_references": [],
  "unclear_items": []
}
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

        # Always use Ollama for source analysis (large payloads)
        import os as _os
        llm = create_llm(
            provider="ollama",
            model=_os.getenv("OLLAMA_MODEL", "llama3:8b"),
            temperature=self._temperature,
            max_tokens=2000,
        )

        # Format source code for the LLM
        source_text = self._format_source_code(state["source_code"])
        call_tree_text = self._format_call_tree(state["call_tree"])

        system_prompt = EXPLANATION_SYSTEM_PROMPT
        # Ollama doesn't need the anthropic-specific instruction
        if False:  # kept for reference
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

        # Invoke with sync-in-thread (Windows SelectorEventLoop compat)
        response = await asyncio.to_thread(
            llm.invoke,
            messages,
        )

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
        provider: Optional[str] = None,  # Ignored — always uses Ollama
        model: Optional[str] = None,  # Ignored — always uses Ollama
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

        # Always use Ollama for source analysis — large payloads break
        # corporate network TLS with remote APIs (OpenAI/Anthropic)
        import os
        llm = create_llm(
            provider="ollama",
            model=os.getenv("OLLAMA_MODEL", "llama3:8b"),
            temperature=self._temperature,
            max_tokens=4096,
        )

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

        system_prompt = SEMANTIC_EXPLANATION_PROMPT
        if (provider or "").lower() == "anthropic":
            system_prompt += (
                "\n\nIMPORTANT: Respond with ONLY the raw JSON object. "
                "No markdown code fences, no explanation before or after."
            )

        user_prompt = (
            f"User Question: {query}\n\n"
            f"The following {len(multi_source)} functions were found via semantic search:\n\n"
            + "\n".join(function_sections)
            + "\n\nAnswer the user's question by explaining the relevant logic across these functions. "
            "Cite specific function names and line numbers for every claim."
        )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]

        response = await llm.ainvoke(messages)

        raw_content = response.content.strip()
        if raw_content.startswith("```"):
            raw_content = raw_content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        explanation = json.loads(raw_content)
        state["explanation"] = explanation

        logger.info(
            f"Semantic explanation generated: "
            f"{len(explanation.get('relevant_functions', []))} functions analyzed | "
            f"correlation_id={correlation_id}"
        )
        return state

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
