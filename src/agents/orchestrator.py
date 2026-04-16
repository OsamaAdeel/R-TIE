"""
RTIE Orchestrator Agent.

Handles query classification and command routing. Determines whether
user input is a slash command or a logic query, and extracts structured
metadata using an LLM with strict JSON output. All queries are routed
through semantic search — the orchestrator simply validates and
prepares the query for the pipeline.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage

from src.graph.state import LogicState
from src.llm_factory import create_llm
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id

logger = get_logger(__name__, concern="app")


class ClassificationResult(BaseModel):
    """Pydantic model for LLM classification output.

    Attributes:
        query_type: 'COLUMN_LOGIC' or 'VARIABLE_TRACE'.
        intent: What the user is asking about.
        search_terms: Key terms for semantic search enrichment.
        target_variable: Variable/column name for VARIABLE_TRACE queries.
        schema_name: Oracle schema name (e.g. OFSMDM).
        confidence: Model's confidence in understanding the query.
    """

    model_config = {"strict": True}

    query_type: str
    intent: str
    search_terms: List[str]
    target_variable: Optional[str] = None
    schema_name: str
    confidence: float


class CommandResult(BaseModel):
    """Parsed slash command result.

    Attributes:
        is_command: Whether the input was a slash command.
        command: The command name (e.g. 'refresh-cache').
        args: List of command arguments.
    """

    model_config = {"strict": True}

    is_command: bool
    command: str
    args: List[str]


CLASSIFICATION_SYSTEM_PROMPT = """You are a query classifier for the RTIE system (Regulatory Trace & Intelligence Engine).
Your job is to understand user queries about Oracle OFSAA PL/SQL objects, tables, columns, and data flows.

You MUST respond with ONLY a valid JSON object — no markdown, no explanation, no extra text.

{
  "query_type": "COLUMN_LOGIC" or "VARIABLE_TRACE",
  "intent": "<concise description of what the user wants to know>",
  "search_terms": ["<keyword1>", "<keyword2>", "..."],
  "target_variable": "<variable/column name if query_type is VARIABLE_TRACE, else null>",
  "schema_name": "<Oracle schema name, default OFSMDM>",
  "confidence": <float between 0.0 and 1.0>
}

Rules:
- query_type: Use "VARIABLE_TRACE" when the user asks how a specific variable or column
  is CALCULATED, DERIVED, POPULATED, or TRANSFORMED across functions.
  Use "COLUMN_LOGIC" for all other logic/explanation queries.
- target_variable: When query_type is "VARIABLE_TRACE", extract the exact variable or
  column name being traced (e.g. "EAD_AMOUNT", "N_ANNUAL_GROSS_INCOME", "V_PROD_CODE").
  Set to null for COLUMN_LOGIC queries.
- intent: summarize what the user is asking in one sentence.
- search_terms: extract ALL relevant keywords — function names, table names, column names,
  business concepts (e.g. "operational risk", "capital adequacy", "GL data").
  These terms will be used for semantic search, so be thorough.
- schema_name defaults to "OFSMDM" unless the user specifies another schema.
- confidence reflects how well you understood the user's question.

Examples:
- "Explain FN_LOAD_OPS_RISK_DATA" → query_type: "COLUMN_LOGIC", target_variable: null
- "How is EAD_AMOUNT calculated across functions?" → query_type: "VARIABLE_TRACE", target_variable: "EAD_AMOUNT"
- "Trace N_ANNUAL_GROSS_INCOME" → query_type: "VARIABLE_TRACE", target_variable: "N_ANNUAL_GROSS_INCOME"
- "What updates STG_PRODUCT_PROCESSOR?" → query_type: "COLUMN_LOGIC", target_variable: null
- "How is V_PROD_CODE populated?" → query_type: "VARIABLE_TRACE", target_variable: "V_PROD_CODE"
- "How does the entire batch flow work?" → query_type: "COLUMN_LOGIC", target_variable: null
"""


class Orchestrator:
    """Orchestrator agent for query classification and command routing.

    Classifies incoming queries and extracts search terms for semantic
    search. Supports dynamic model switching between OpenAI and Claude.
    """

    def __init__(
        self,
        temperature: float = 0,
        max_tokens: int = 2000,
    ) -> None:
        """Initialize the Orchestrator with LLM settings.

        Args:
            temperature: LLM temperature. Defaults to 0.
            max_tokens: Maximum tokens for LLM response. Defaults to 2000.
        """
        self._temperature = temperature
        self._max_tokens = max_tokens

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

    def check_command(self, query: str) -> CommandResult:
        """Check if the query is a slash command.

        Parses queries starting with '/' into command name and arguments.

        Args:
            query: The raw user query string.

        Returns:
            CommandResult with is_command=True and parsed command/args if
            the query starts with '/', otherwise is_command=False.
        """
        query = query.strip()
        if not query.startswith("/"):
            logger.info(f"Query is not a command: {query[:50]}...")
            return CommandResult(is_command=False, command="", args=[])

        parts = query.split()
        command = parts[0].lstrip("/")
        args = parts[1:] if len(parts) > 1 else []

        logger.info(
            f"Command detected: /{command} with args: {args} | "
            f"correlation_id={get_correlation_id()}"
        )
        return CommandResult(is_command=True, command=command, args=args)

    async def classify_query(
        self,
        query: str,
        state: LogicState,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LogicState:
        """Classify a query and extract search terms for semantic search.

        Sends the query to the LLM to extract intent and search terms.
        These terms enrich the semantic search embedding for better results.

        Args:
            query: The raw user query string.
            state: Current LogicState to update.
            provider: LLM provider. None uses default.
            model: Specific model name. None uses default.

        Returns:
            Updated LogicState with query_type, object_name, schema populated.
        """
        correlation_id = get_correlation_id()
        logger.info(
            f"Classifying query: {query[:80]}... | "
            f"provider={provider}, model={model} | "
            f"correlation_id={correlation_id}"
        )

        llm = self._get_llm(provider, model)

        system_prompt = CLASSIFICATION_SYSTEM_PROMPT
        if (provider or "").lower() == "anthropic":
            system_prompt += (
                "\n\nIMPORTANT: Respond with ONLY the raw JSON object. "
                "No markdown code fences, no explanation before or after."
            )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=query),
        ]

        response = await llm.ainvoke(messages)
        raw_content = response.content.strip()

        # Strip markdown fences if present
        if raw_content.startswith("```"):
            raw_content = raw_content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        logger.info(
            f"LLM classification response: {raw_content} | "
            f"correlation_id={correlation_id}"
        )

        parsed = json.loads(raw_content)
        result = ClassificationResult(**parsed)

        # Build enriched search query from intent + search terms
        enriched_query = f"{query} {result.intent} {' '.join(result.search_terms)}"

        state["query_type"] = result.query_type
        state["object_name"] = enriched_query
        state["object_type"] = ""
        state["schema"] = result.schema_name
        state["target_variable"] = result.target_variable or ""
        state["warnings"] = []
        state["partial_flag"] = False

        logger.info(
            f"Query classified: type={result.query_type}, "
            f"intent='{result.intent}', "
            f"target_variable={result.target_variable}, "
            f"search_terms={result.search_terms}, "
            f"schema={result.schema_name}, "
            f"confidence={result.confidence} | "
            f"correlation_id={correlation_id}"
        )
        return state
