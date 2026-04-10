"""
RTIE Orchestrator Agent.

Handles query classification and command routing. Determines whether
user input is a slash command or a logic query, and extracts structured
metadata (object name, schema, query type) using an LLM with strict
JSON output. Supports dynamic model switching between OpenAI and Claude.
"""

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, model_validator
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
        query_type: Either 'COLUMN_LOGIC' or 'COMMAND'.
        object_name: Name of the PL/SQL object to analyze.
        schema: Oracle schema name (e.g. OFSMDM).
        confidence: Model's confidence in the classification (0.0 - 1.0).
    """

    model_config = {"strict": True}

    query_type: str
    object_name: str
    schema: str
    confidence: float

    @model_validator(mode="after")
    def validate_query_type(self) -> "ClassificationResult":
        """Ensure query_type is one of the allowed values.

        Returns:
            Self if valid.

        Raises:
            ValueError: If query_type is not COLUMN_LOGIC or COMMAND.
        """
        if self.query_type not in ("COLUMN_LOGIC", "COMMAND"):
            raise ValueError(
                f"query_type must be 'COLUMN_LOGIC' or 'COMMAND', got '{self.query_type}'"
            )
        return self


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
Your job is to classify user queries about Oracle OFSAA PL/SQL objects.

You MUST respond with ONLY a valid JSON object — no markdown, no explanation, no extra text.

The JSON must have exactly these fields:
{
  "query_type": "COLUMN_LOGIC",
  "object_name": "<name of the PL/SQL function, procedure, or package>",
  "schema": "<Oracle schema name, default OFSMDM>",
  "confidence": <float between 0.0 and 1.0>
}

Rules:
- query_type is always "COLUMN_LOGIC" for logic explanation queries.
- object_name is the PL/SQL function, procedure, or package name mentioned in the query.
- schema defaults to "OFSMDM" unless the user specifies another schema.
- confidence reflects how certain you are about the classification.
- If you cannot identify a clear object name, set confidence below 0.7.
- Do NOT invent object names — only use names explicitly mentioned in the query.
"""


class Orchestrator:
    """Orchestrator agent for query classification and command routing.

    Classifies incoming queries as either slash commands or logic
    explanation requests. Supports dynamic model switching between
    OpenAI and Anthropic (Claude) per request.
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
        """Classify a logic query using the selected LLM.

        Sends the query to the LLM with a strict system prompt that forces
        JSON output. Validates the response with Pydantic. If confidence
        is below 0.7, returns a clarification request instead.

        Args:
            query: The raw user query string.
            state: Current LogicState to update with classification results.
            provider: LLM provider ('openai' or 'anthropic'). None uses default.
            model: Specific model name. None uses default for provider.

        Returns:
            Updated LogicState with query_type, object_name, schema, and
            confidence populated. If confidence is too low, output contains
            a clarification request.
        """
        correlation_id = get_correlation_id()
        logger.info(
            f"Classifying query: {query[:80]}... | "
            f"provider={provider}, model={model} | "
            f"correlation_id={correlation_id}"
        )

        llm = self._get_llm(provider, model)

        # For Anthropic, embed JSON instruction more explicitly since no json_mode
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

        # Strip markdown fences if present (Claude sometimes adds them)
        if raw_content.startswith("```"):
            raw_content = raw_content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        logger.info(
            f"LLM classification raw response: {raw_content} | "
            f"correlation_id={correlation_id}"
        )

        # Parse and validate with Pydantic
        parsed = json.loads(raw_content)
        result = ClassificationResult(**parsed)

        # Check confidence threshold
        if result.confidence < 0.7:
            logger.warning(
                f"Low confidence classification ({result.confidence}): {query[:50]}... | "
                f"correlation_id={correlation_id}"
            )
            state["query_type"] = result.query_type
            state["object_name"] = result.object_name
            state["schema"] = result.schema
            state["output"] = {
                "type": "clarification",
                "message": (
                    f"I'm not confident enough (confidence: {result.confidence:.2f}) "
                    f"about which object you're referring to. Could you please specify "
                    f"the exact function or procedure name?"
                ),
            }
            state["partial_flag"] = True
            return state

        state["query_type"] = result.query_type
        state["object_name"] = result.object_name
        state["schema"] = result.schema
        state["warnings"] = []
        state["partial_flag"] = False

        logger.info(
            f"Query classified: type={result.query_type}, "
            f"object={result.object_name}, schema={result.schema}, "
            f"confidence={result.confidence} | correlation_id={correlation_id}"
        )
        return state
