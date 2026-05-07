"""
RTIE Logic Explainer Agent.

Uses an LLM (OpenAI or Claude) to generate structured, fully-cited
explanations of PL/SQL functions and procedures. Every claim in the
explanation must reference specific line numbers from the source code.
LangSmith tracing is enabled on all LLM calls. Supports dynamic model
switching per request.
"""

import json
import re
from typing import Any, Dict, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage

from src.pipeline.state import LogicState
from src.llm_factory import create_llm
from src.llm_errors import sanitize_llm_exception
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id
from src.parsing.schema_discovery import fallback_to_default_schema

logger = get_logger(__name__, concern="app")

# Phrases that signal the LLM is flagging missing information. If the model
# emits one of these AND then continues to generate substantive text, we
# treat the response as self-contradictory and downgrade the badge.
_FORBIDDEN_CONTRADICTION_PHRASES = (
    "source not provided",
    "source not available",
    "i cannot determine",
    "i do not have access",
    "source was not included",
    "could not locate",
    "was not provided",
)

# Business-identifier pattern: "CAP973", "ABL013" etc. — at least two letters
# followed by at least two digits, optionally with trailing alphanumerics.
_IDENTIFIER_CODE_RE = re.compile(r"\b([A-Z]{2,}[0-9]{2,}[A-Z0-9]*)\b")

# Inline line references in markdown: "Line 203", "Lines 5-10", "L42".
_LINE_REF_RE = re.compile(
    r"\b(?:Lines?|L)\s*(\d+)(?:\s*[-\u2013]\s*(\d+))?\b"
)

# Query types for which grounded citations are expected. Other types
# (DATA_QUERY, VALUE_TRACE, etc.) have their own validation paths.
_REQUIRES_CITATIONS = frozenset({"VARIABLE_TRACE", "COLUMN_LOGIC", "FUNCTION_LOGIC"})


# W35 Phase 7: derivation banner template. Rendered programmatically (NOT
# via the LLM) when the orchestrator's BI routing resolved an identifier
# to a function whose Phase 6 derivation summary is available. The
# wording is owned by RTIE so the user always sees the same shape for
# the same arithmetic.
_DERIVATION_HEADER_TEMPLATE = (
    "## Derivation\n\n"
    "**{formula}**\n\n"
    "{description}\n\n"
)

# Per-operation natural-language description used inside the banner.
# Operands are referenced by name (target_literal, source_literals[0],
# source_literals[1], etc.) so adding a new operation only requires a
# new entry here.
_DERIVATION_OP_DESCRIPTIONS = {
    "SUBTRACT": (
        "This value is computed in {function} ({schema} schema) by "
        "subtracting {b} from {a}."
    ),
    "DIRECT_ASSIGN": (
        "This value is assigned in {function} ({schema} schema) directly "
        "from {a}."
    ),
}


def render_derivation_header(state: LogicState) -> str:
    """Render the structured Derivation banner for a BI-routed query.

    Reads ``state["bi_routing"]`` (set by
    :func:`src.agents.orchestrator.apply_bi_routing`). Returns an empty
    string when:
      - BI routing did not fire (``bi_routing`` is missing/empty)
      - the routed function had no Phase 6 derivation summary
      - the operation kind is not one we know how to format

    The wording is rendered programmatically — the LLM does NOT generate
    this banner. The hierarchy header (W39 behaviour) renders ABOVE the
    derivation banner; the section ordering is hierarchy -> derivation
    -> step-by-step body.

    Args:
        state: Current pipeline state.

    Returns:
        Markdown string ready to prepend to the explanation, or ``""``.
    """
    bi = state.get("bi_routing") or {}
    derivation = bi.get("derivation") or {}
    if not derivation:
        return ""

    target = (bi.get("identifier") or "").strip()
    function = (bi.get("function") or "").strip()
    schema = (bi.get("schema") or "").strip()
    operation = (derivation.get("operation") or "").strip().upper()
    sources = list(derivation.get("source_literals") or [])
    if not target or not function or not operation:
        return ""

    desc_template = _DERIVATION_OP_DESCRIPTIONS.get(operation)
    if desc_template is None:
        return ""

    if operation == "SUBTRACT":
        if len(sources) < 2:
            return ""
        formula = f"{target} = {sources[0]} - {sources[1]}"
        description = desc_template.format(
            function=function, schema=schema, a=sources[0], b=sources[1],
        )
    elif operation == "DIRECT_ASSIGN":
        if len(sources) < 1:
            return ""
        formula = f"{target} is assigned the value of {sources[0]}"
        description = desc_template.format(
            function=function, schema=schema, a=sources[0],
        )
    else:
        # Defensive — _DERIVATION_OP_DESCRIPTIONS gate above should make
        # this branch unreachable.
        return ""

    return _DERIVATION_HEADER_TEMPLATE.format(
        formula=formula, description=description,
    )


def evaluate_grounding(
    raw_query: str,
    markdown: str,
    multi_source: Dict[str, Any],
    functions_analyzed: List[str],
    query_type: str,
    redis_client: Any = None,
) -> Dict[str, Any]:
    """Evaluate whether a streamed explanation is grounded in retrieved source.

    Runs four independent checks:
      - forbidden-phrase self-contradiction
      - business-identifier grounding (CAP codes, etc.)
      - line-citation presence
      - empty source_citations rule for logic-explaining query types

    Phase 4: when *redis_client* is provided, the identifier-grounding
    check uses the same multi-schema backstop as
    :func:`detect_ungrounded_identifiers` — an identifier present in any
    schema's source body is treated as grounded even when the local
    multi_source didn't include it (so the post-hoc warning matches the
    pre-generation routing decision).

    Returns a dict with keys ``badge`` (VERIFIED | UNVERIFIED), ``confidence``,
    ``source_citations`` (line-reference stubs), ``warnings`` (machine-readable),
    and ``sanity_messages`` (user-facing caveats that the caller should append
    to the streamed response).
    """
    warnings: List[str] = []
    sanity_messages: List[str] = []

    citations = _extract_line_citations(markdown)

    if _has_self_contradiction(markdown):
        warnings.append(
            "CONTRADICTION: response claims missing information but continues "
            "to provide substantive explanation"
        )
        sanity_messages.append(
            "The response appears to contradict itself — it states information "
            "is missing but then provides it. Please verify against the "
            "production code before relying on this explanation."
        )

    query_identifiers = set(_IDENTIFIER_CODE_RE.findall(raw_query.upper()))
    if query_identifiers:
        raw_source_text = _concat_multi_source(multi_source).upper()
        local_ungrounded = [
            ident for ident in query_identifiers if ident not in raw_source_text
        ]
        if local_ungrounded and redis_client is not None:
            from src.parsing.schema_discovery import (
                identifier_grounded_in_any_schema,
            )
            kept: List[str] = []
            for ident in local_ungrounded:
                try:
                    if identifier_grounded_in_any_schema(ident, redis_client):
                        continue
                except Exception:
                    pass
                kept.append(ident)
            ungrounded = sorted(kept)
        else:
            ungrounded = sorted(local_ungrounded)
        if ungrounded:
            ident_list = ", ".join(ungrounded)
            warnings.append(
                f"UNGROUNDED_IDENTIFIERS: {ident_list} mentioned in query but "
                f"not found in any loaded function source"
            )
            sanity_messages.append(
                f"This explanation may not fully describe {ident_list}. The "
                f"identifier was mentioned but no loaded function was confirmed "
                f"to compute it. The explanation below reflects what the loaded "
                f"functions do — please verify against the actual production "
                f"code."
            )

    # Requested-function grounding: if the user named a specific PL/SQL
    # function and it didn't make it into functions_analyzed, the semantic
    # search produced adjacent functions instead of the real one. This
    # catches the exact W37 failure mode where the vector store doesn't
    # index a schema (e.g. OFSERM) but the graph does have it — the
    # pre-check passes but semantic search silently substitutes neighbors.
    requested_functions = _extract_function_candidates_local(raw_query)
    if requested_functions:
        analyzed_upper = {f.upper() for f in functions_analyzed}
        missing = [
            name for name in requested_functions
            if name.upper() not in analyzed_upper
        ]
        if missing:
            names = ", ".join(missing)
            warnings.append(
                f"NAMED_FUNCTION_NOT_RETRIEVED: {names} named in query but not "
                f"present in functions_analyzed={list(analyzed_upper)}"
            )
            sanity_messages.append(
                f"The explanation below may describe functions related to "
                f"{names} rather than {names} itself — the semantic search "
                f"returned different functions than the one you asked about. "
                f"Please verify against the actual production code."
            )

    requires_citations = query_type in _REQUIRES_CITATIONS
    # A response is "citationally grounded" if it either has explicit line
    # references OR analyzed at least one function (which implies the LLM
    # was given real source to work from).
    has_citations = bool(citations) or bool(functions_analyzed)

    # W57: post-generation grounding enforcement. Runs six independent
    # content checks (per-claim binding, citation cap, anchoring, chain
    # coherence, hierarchy/body consistency, template phrases, self-aware
    # caveat) and appends warnings for any that fire. Each W57 warning
    # carries its severity in the prefix: GROUNDING-HIGH: blocks the
    # badge; GROUNDING-LOW: is advisory only. Pre-existing warnings
    # (CONTRADICTION, UNGROUNDED_IDENTIFIERS, NAMED_FUNCTION_NOT_RETRIEVED,
    # CITATIONS, etc.) keep their prior blocking behavior.
    if query_type in _REQUIRES_CITATIONS:
        try:
            w57_warnings = w57_enforce_grounding(
                raw_query=raw_query,
                markdown=markdown,
                multi_source=multi_source,
                functions_analyzed=functions_analyzed,
                redis_client=redis_client,
            )
            warnings.extend(w57_warnings)
        except Exception as exc:
            # Never fail the response on a grounding-check exception.
            # Log and continue with the existing checks' verdict.
            logger.warning(
                "W57 enforcement raised an exception; falling back to "
                "pre-W57 grounding verdict: %s", exc,
            )

    # A warning blocks (forces UNVERIFIED) unless it's a W57 LOW-severity
    # advisory. Padding-only signals (range-repeat, citation-count cap)
    # ride along under GROUNDING-LOW: and don't flip the badge — they
    # surface to the user via the TrustBanner as a citation-hygiene
    # advisory while VERIFIED stands.
    blocking_warnings = [
        w for w in warnings if not w.startswith("GROUNDING-LOW:")
    ]

    if requires_citations and not has_citations:
        badge = "UNVERIFIED"
        confidence = 0.0
        warnings.append(
            "CITATIONS: response has no line references and no functions were "
            "analyzed — cannot verify grounding"
        )
    elif blocking_warnings:
        badge = "UNVERIFIED"
        confidence = 0.4 if citations else 0.2
    else:
        badge = "VERIFIED"
        confidence = 0.95 if citations else 0.8

    return {
        "badge": badge,
        "confidence": confidence,
        "source_citations": citations,
        "warnings": warnings,
        "sanity_messages": sanity_messages,
    }


def detect_ungrounded_identifiers(
    raw_query: str,
    multi_source: Dict[str, Any],
    redis_client: Any = None,
) -> List[str]:
    """Return business identifiers named in the query but absent from every
    retrieved function's source_code body.

    Phase 4: when *redis_client* is provided, after the multi_source check
    runs an additional cross-schema scan over ``graph:source:*`` keys and
    drops any identifier that DOES appear in some loaded function's body
    in any discovered schema. The cross-schema scan turns the local
    "missing from this query's top-K candidates" check into a global
    "missing from every loaded function in every schema" backstop, which
    is what W45 truly cares about. Pre-Phase-4 callers (``redis_client``
    omitted) get the original Phase-1/2/3 behaviour unchanged.

    Uses the same identifier regex and matching logic as
    evaluate_grounding so the pre-generation branch and the post-hoc
    backstop always agree on which identifiers are ungrounded. Call this
    BEFORE the LLM generation step to decide whether to route to the
    ungrounded branch.

    An empty list means either (a) the query contains no business
    identifiers, or (b) every identifier is present in at least one
    retrieved function or somewhere in any schema's source bodies.
    """
    query_identifiers = set(_IDENTIFIER_CODE_RE.findall(raw_query.upper()))
    if not query_identifiers:
        return []
    source_text = _concat_multi_source(multi_source).upper()
    locally_ungrounded = [
        ident for ident in query_identifiers if ident not in source_text
    ]
    if not locally_ungrounded or redis_client is None:
        return sorted(locally_ungrounded)

    # Phase 4 multi-schema backstop: drop identifiers found anywhere in
    # the cross-schema source corpus. Imported lazily to avoid a circular
    # import (schema_discovery -> store -> [...] -> logic_explainer when
    # the explainer is reloaded by tests).
    from src.parsing.schema_discovery import identifier_grounded_in_any_schema

    truly_ungrounded: List[str] = []
    for ident in locally_ungrounded:
        try:
            if identifier_grounded_in_any_schema(ident, redis_client):
                logger.info(
                    "W45 backstop: identifier %s found in cross-schema "
                    "source corpus; suppressing ungrounded flag",
                    ident,
                )
                continue
        except Exception as exc:
            logger.warning(
                "W45 multi-schema backstop check failed for %s: %s",
                ident, exc,
            )
        truly_ungrounded.append(ident)
    return sorted(truly_ungrounded)


# Minimum source-body length (in characters) below which we consider a
# function's retrieved source effectively empty. A real PL/SQL function body
# even for a one-liner has a CREATE/BEGIN/END structure well over 50 chars,
# so anything shorter is treated as "no real source available".
_PARTIAL_SOURCE_MIN_CHARS = 50


def detect_partial_source_function(
    function_name: str,
    schema: str,
    retrieved_source: Any,
    redis_client: Any = None,
) -> bool:
    """Return True when *function_name* has graph metadata but no usable
    source body to feed the LLM.

    This is the W49 partial-indexed state: the function name and hierarchy
    are known (``graph:meta:<schema>:<function_name>`` exists), but
    semantic search / source retrieval did not return its PL/SQL body. The
    response generator must NOT speculate using related functions when
    this is true.

    Args:
        function_name: The asked-about function (case-insensitive).
        schema: Schema to check for parse metadata. Pass empty string to
            skip the metadata check (caller already verified).
        retrieved_source: The source body returned for *function_name* by
            the pipeline. Acceptable shapes mirror ``multi_source`` entries:
            ``None``, an empty string, a list of dicts ``[{"line": N,
            "text": "..."}]``, or a list of strings. Treated as missing
            when the joined text is below ``_PARTIAL_SOURCE_MIN_CHARS``.
        redis_client: Redis client used to verify metadata presence. When
            ``None``, the check falls open (returns False) to avoid a
            false positive on a misconfigured environment.

    Returns:
        True only when both conditions hold:
          - graph metadata exists for (schema, function_name)
          - retrieved_source is missing/empty/below threshold

    Pure-ish function: no LLM calls, only a single Redis GET on the
    parse_metadata key. Reuses the existing client connection.
    """
    if not function_name:
        return False
    if redis_client is None:
        return False

    body_len = _retrieved_source_length(retrieved_source)
    if body_len >= _PARTIAL_SOURCE_MIN_CHARS:
        return False

    try:
        from src.parsing.store import get_parse_metadata
        metadata = get_parse_metadata(
            redis_client, schema, function_name.upper()
        )
    except Exception as exc:
        logger.debug(
            "partial-source metadata lookup failed for %s.%s: %s",
            schema, function_name, exc,
        )
        return False
    return metadata is not None


def _retrieved_source_length(retrieved_source: Any) -> int:
    """Return the joined character length of *retrieved_source*.

    Handles the same shapes as ``_concat_multi_source`` (list of dicts,
    list of strings, plain string, None). Whitespace-only content
    collapses to length 0 so it triggers the partial-source path.
    """
    if retrieved_source is None:
        return 0
    if isinstance(retrieved_source, str):
        return len(retrieved_source.strip())
    if isinstance(retrieved_source, list):
        parts: List[str] = []
        for item in retrieved_source:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return len(" ".join(parts).strip())
    return 0


def _extract_line_citations(markdown: str) -> List[Dict[str, Any]]:
    """Return citation stubs for every Line-N reference found in *markdown*.

    Ranges like "Lines 203-223" expand into one stub per line. De-duplicated
    by line number so a heavily-cited line only shows up once.
    """
    citations: List[Dict[str, Any]] = []
    seen: set[int] = set()
    for match in _LINE_REF_RE.finditer(markdown):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start or end - start > 500:
            # Guard against degenerate regex matches (huge spans, reversed).
            continue
        for line_num in range(start, end + 1):
            if line_num in seen:
                continue
            seen.add(line_num)
            citations.append({
                "line": line_num,
                "text": "",
                "context": "inline reference",
                "source": "markdown",
            })
    return citations


def _has_self_contradiction(markdown: str) -> bool:
    """Return True if a forbidden phrase precedes >50 tokens of continuation."""
    low = markdown.lower()
    for phrase in _FORBIDDEN_CONTRADICTION_PHRASES:
        idx = low.find(phrase)
        if idx < 0:
            continue
        rest = markdown[idx + len(phrase):]
        tokens = [t for t in re.split(r"\s+", rest) if t]
        if len(tokens) > 50:
            return True
    return False


def _concat_multi_source(multi_source: Dict[str, Any]) -> str:
    """Flatten every function's source_code lines into one searchable string."""
    parts: List[str] = []
    for fn_data in multi_source.values():
        src = fn_data.get("source_code") or []
        for item in src:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
    return " ".join(parts)


# Local copy of the function-name extractor used during grounding evaluation.
# Kept in sync with src.agents.orchestrator.extract_function_candidates —
# duplicated here to avoid an import cycle (orchestrator imports from store,
# grounding is called from main.py after orchestrator has already run).
_FN_CANDIDATE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)\b")
_FN_COLUMN_PREFIX_RE = re.compile(r"^[A-Z]_[A-Z]")
_FN_STOPWORDS = frozenset({
    "FIC_MIS_DATE", "MIS_DATE", "RUN_ID", "BATCH_ID", "RUN_SKEY",
    "RUN_EXECUTION_ID", "START_DATE", "END_DATE", "ACCOUNT_NUMBER",
    "TARGET_VARIABLE", "STG_GL_DATA", "V_GL_CODE", "V_PROD_CODE",
    "V_LOB_CODE", "V_LV_CODE",
})


def _extract_function_candidates_local(query: str) -> List[str]:
    """Same heuristic as orchestrator.extract_function_candidates; duplicated
    here to avoid an import cycle during grounding evaluation."""
    seen: set[str] = set()
    out: List[str] = []
    for match in _FN_CANDIDATE_RE.finditer(query):
        cand = match.group(1)
        cu = cand.upper()
        if cu in seen:
            continue
        seen.add(cu)
        if len(cand) < 6:
            continue
        if cu in _FN_STOPWORDS:
            continue
        if _FN_COLUMN_PREFIX_RE.match(cu):
            continue
        out.append(cand)
    return out


# ---------------------------------------------------------------------------
# W57: post-generation grounding enforcement
# ---------------------------------------------------------------------------
#
# Six independent checks that run AFTER the response markdown is generated
# and BEFORE the badge is finalized. Any check that returns warnings flows
# through the existing badge-decision logic in evaluate_grounding, which
# downgrades VERIFIED → UNVERIFIED whenever warnings is non-empty.
#
# Defense-in-depth: false-positives are preferable to false-negatives — the
# trust contract requires UNVERIFIED whenever a check is uncertain.

# Citation patterns:
#   Check 1 uses _LINE_REF_RE (already defined) for "(start, end)" tuples
#   so we can count repeats and detect padding fabrications. Function-name
#   binding is enforced against functions_analyzed: a citation is "bound"
#   when at least one function was actually analyzed (which means the LLM
#   was given real source). For the per-claim function-name regex pattern
#   like "(FN_NAME, Lines X-Y)", the parenthesized form below is used.
_W57_FUNC_CITATION_RE = re.compile(
    r"\(\s*([A-Za-z][A-Za-z0-9_]+)\s*,\s*Lines?\s+(\d+)"
    r"(?:\s*[-–]\s*(\d+))?\s*\)"
)

# Check 1.3 threshold: a single (start,end) range cited more than this
# many times signals line-by-line padding rather than per-claim binding.
# Empirically: legitimate answers cite a range 1-3 times; fabricated
# answers showed counts of 6, 172, 296 across benchmark Run 3.
_W57_RANGE_REPEAT_THRESHOLD = 3

# Check 2 cap: total distinct line references in a response. Legitimate
# answers from Run 3 ranged 1-30; fabricated answers were 172, 296, 296.
# 50 is well above the highest legitimate count and well below the
# fabrication floor, leaving generous headroom for long real answers.
_W57_CITATION_CAP = 50

# Check 3a anchoring threshold: when the user named a specific function,
# the most-cited function must not be referenced more than this multiple
# of the asked-about function's citation count. Catches the C2 failure
# (user asked CSTM, response cited a different family 100x more).
_W57_PRIMARY_DOMINANCE_RATIO = 2

# Word-boundary patterns used by Check 5's "no internal gating" validator.
# Compiled once at module load to keep the predicate lambdas cheap.
_W57_GATING_IF_RE = re.compile(r"\bIF\b", re.IGNORECASE)
_W57_GATING_WHEN_RE = re.compile(r"\bWHEN\b", re.IGNORECASE)
_W57_GATING_CASE_RE = re.compile(r"\bCASE\b", re.IGNORECASE)

# Check 5 template phrases. Each entry is (phrase, validator) where
# validator(source_text) returns True iff the source actually supports
# the claim. Phrases that don't appear in any cited source mean the
# model produced a generic template without reading the body.
_W57_TEMPLATE_PHRASES = (
    (
        "only runs when the reporting month is december",
        lambda src: ("EXTRACT(MONTH" in src.upper() or
                     "TO_CHAR" in src.upper()) and "12" in src,
    ),
    (
        "only runs in december",
        lambda src: ("EXTRACT(MONTH" in src.upper() or
                     "TO_CHAR" in src.upper()) and "12" in src,
    ),
    (
        "no internal gating",
        lambda src: ("EXTRACT(MONTH" not in src.upper() and
                     not _W57_GATING_IF_RE.search(src) and
                     not _W57_GATING_WHEN_RE.search(src) and
                     not _W57_GATING_CASE_RE.search(src)),
    ),
    (
        "only runs march 2026",
        lambda src: "20260331" in src or "MARCH 2026" in src.upper(),
    ),
    (
        "pass-through",
        lambda src: src.upper().count("INSERT INTO") <= 1
                     and "MERGE" not in src.upper(),
    ),
)

# Check 6 caveat triggers: phrases that the system itself emits when it
# already knows the answer is uncertain. If any are present in the
# rendered markdown, the badge MUST reflect that uncertainty.
_W57_CAVEAT_TRIGGERS = (
    "may describe functions related to",
    "semantic search returned different functions",
    "may not be the actual function you asked about",
    "verify against the actual production code",
)


def _w57_extract_ranges(markdown: str) -> List[tuple]:
    """Return list of (start, end) line-range tuples for every citation.

    Unlike :func:`_extract_line_citations` which expands ranges into
    per-line stubs, this preserves the citation-event granularity so we
    can count repeats and detect padding fabrications.
    """
    ranges: List[tuple] = []
    for match in _LINE_REF_RE.finditer(markdown):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start or end - start > 500:
            continue
        ranges.append((start, end))
    return ranges


def _w57_check_per_claim_binding(
    markdown: str,
    multi_source: Dict[str, Any],
    functions_analyzed: List[str],
) -> List[str]:
    """W57 Check 1: per-claim citation binding.

    Three sub-checks:
      1.1 Each explicit (function_name, line_range) tuple in the markdown
          must reference a function in retrieved sources.
      1.2 Each line range must fit inside that function's source body.
      1.3 The same line range repeated more than
          ``_W57_RANGE_REPEAT_THRESHOLD`` times across the response is
          line-by-line padding (fabrication signal).
    """
    warnings: List[str] = []
    sources_upper = {fn.upper(): _source_line_count(multi_source.get(fn))
                     for fn in multi_source}

    # 1.1 + 1.2: function-name-bound citations
    for match in _W57_FUNC_CITATION_RE.finditer(markdown):
        fn_name = match.group(1)
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) else start
        fn_upper = fn_name.upper()
        if fn_upper not in sources_upper:
            warnings.append(
                f"GROUNDING-HIGH: cited function '{fn_name}' not in retrieved "
                f"sources (analyzed: {sorted(sources_upper.keys())[:5]})"
            )
            continue
        max_line = sources_upper[fn_upper]
        if max_line and (start > max_line or end > max_line):
            warnings.append(
                f"GROUNDING-HIGH: cited range {start}-{end} for '{fn_name}' "
                f"exceeds source length ({max_line} lines)"
            )

    # 1.3: range-repeat threshold (uses every Line-N reference, not just
    # the parenthesised form, so it catches line-by-line padding even
    # when no function name is bound).
    from collections import Counter
    range_counts = Counter(_w57_extract_ranges(markdown))
    for (start, end), count in range_counts.items():
        if count > _W57_RANGE_REPEAT_THRESHOLD:
            label = f"Lines {start}-{end}" if end != start else f"Line {start}"
            warnings.append(
                f"GROUNDING-LOW: {label} cited {count} times "
                f"(threshold {_W57_RANGE_REPEAT_THRESHOLD}); likely "
                f"line-by-line padding rather than per-claim binding"
            )
    return warnings


def _source_line_count(fn_data: Any) -> int:
    """Return the highest line number stored for a function in multi_source.

    Returns 0 when ``fn_data`` is None, has no source_code, or stores
    plain strings without explicit line numbers (in which case the range
    check at 1.2 is skipped because we have no ground truth to compare).
    """
    if not isinstance(fn_data, dict):
        return 0
    src = fn_data.get("source_code") or []
    if not isinstance(src, list):
        return 0
    max_line = 0
    has_line = False
    for item in src:
        if isinstance(item, dict):
            line = item.get("line")
            if isinstance(line, int):
                has_line = True
                if line > max_line:
                    max_line = line
    return max_line if has_line else 0


def _w57_check_citation_count_cap(markdown: str) -> List[str]:
    """W57 Check 2: total citations capped at ``_W57_CITATION_CAP``.

    Counts citation EVENTS (each Line-N regex match), not the expanded
    line stubs that ``_extract_line_citations`` produces. A response
    above the cap is treated as fabrication regardless of which lines
    it cites.
    """
    citation_events = sum(1 for _ in _LINE_REF_RE.finditer(markdown))
    if citation_events > _W57_CITATION_CAP:
        return [
            f"GROUNDING-LOW: response has {citation_events} line citations "
            f"(cap {_W57_CITATION_CAP}); likely line-by-line padding "
            f"rather than per-claim binding"
        ]
    return []


def _w57_check_anchoring(
    raw_query: str,
    functions_analyzed: List[str],
    markdown: str,
) -> List[str]:
    """W57 Check 3a: when the user named a specific function, the response
    must address it.

    Two failure modes:
      - asked-about function is not in functions_analyzed at all
      - asked-about is analyzed but the response primarily cites a
        different function much more frequently
    """
    asked = _extract_function_candidates_local(raw_query)
    if not asked:
        return []  # nothing to anchor against

    analyzed_upper = {f.upper() for f in functions_analyzed}
    missing = [a for a in asked if a.upper() not in analyzed_upper]
    if missing:
        # NAMED_FUNCTION_NOT_RETRIEVED already covers this case in the
        # main grounding check; W57 doesn't double-emit it. The caveat
        # this triggers will be caught separately by Check 6.
        return []

    # Single-function answer: the asked-about function IS the analyzed
    # function. By construction the response describes it; no anchoring
    # ambiguity is possible. (Avoids false-positive when clean prose
    # cites lines without redundantly inlining the function name.)
    if len(analyzed_upper) <= 1:
        return []

    # Multi-function answer: the asked-about function should not be
    # dominated by another analyzed function in the body. Compute body
    # mention counts and flag if any other function appears
    # _W57_PRIMARY_DOMINANCE_RATIO× more often than the asked-about one.
    asked_primary = asked[0].upper()
    body_upper = markdown.upper()
    asked_count = body_upper.count(asked_primary)
    other_counts = {
        fn: body_upper.count(fn)
        for fn in analyzed_upper
        if fn != asked_primary
    }
    if not other_counts:
        return []
    top_other_fn, top_other_count = max(
        other_counts.items(), key=lambda kv: kv[1]
    )
    # Use max(asked_count, 1) so a body that doesn't name the asked
    # function at all but DOES name another N times still flags when
    # N > ratio. Asked-count of 0 means the body is silent on the
    # asked-about function, which is itself a signal.
    threshold = max(asked_count, 1) * _W57_PRIMARY_DOMINANCE_RATIO
    if top_other_count > threshold:
        return [
            f"GROUNDING-HIGH: user asked about '{asked[0]}' "
            f"({asked_count} mentions) but response primarily cites "
            f"'{top_other_fn}' ({top_other_count} mentions, "
            f">{_W57_PRIMARY_DOMINANCE_RATIO}x ratio)"
        ]
    return []


_W57_STEP_HEADER_RE = re.compile(
    r"^#{1,6}\s+Step\s+(\d+)[^A-Za-z0-9]+([A-Z][A-Z0-9_]+)",
    re.MULTILINE,
)

# Internal-alias / OFSAA-generated identifiers that the step-header regex
# can match but that are NOT real function names. Mirrors the W58
# exclusions in src.agents.orchestrator (EXP_<n>, COND_<n>, T_<n>, SS_,
# TT_) plus a skip for the literal SQL keyword MERGE which is too short
# to be a real OFSAA function name and shows up as a CASE branch label
# in generated MERGE INTO bodies.
_W57_STEP_FN_NOT_REAL_RE = re.compile(
    r"^(EXP_\d|COND_\d|T_\d|SS_|TT_|MERGE\b|INSERT\b|UPDATE\b|SELECT\b)"
)


def _w57_check_chain_coherence(
    markdown: str,
    multi_source: Dict[str, Any],
) -> List[str]:
    """W57 Check 3b: when the response presents N functions as ordered
    steps in a chain, validate the chain is supported by shared tables
    or columns between consecutive steps.

    Signal: response contains multiple "## Step N: FN_NAME" headers
    referring to distinct functions. For each consecutive pair, check
    whether step N writes to or reads from a table that step N+1 also
    references. Lacking shared schema-state, the chain is dubious.
    """
    matches = list(_W57_STEP_HEADER_RE.finditer(markdown))
    if len(matches) < 2:
        return []

    # Preserve order; ignore duplicate function names within steps.
    # Filter out tokens that aren't function names: too-short tokens,
    # OFSAA Hungarian column prefixes (N_X, V_X, etc.), and OFSAA
    # internal aliases (EXP_<n>, COND_<n>, T_<n>, SS_, TT_) plus bare
    # SQL keywords (MERGE / INSERT / UPDATE / SELECT) the regex picks
    # up when a step header introduces an inline statement instead of
    # a function name.
    seen_fns: set[str] = set()
    step_fns: List[str] = []
    for m in matches:
        fn = m.group(2).upper()
        if len(fn) < 6:
            continue
        if _FN_COLUMN_PREFIX_RE.match(fn):
            continue
        if _W57_STEP_FN_NOT_REAL_RE.match(fn):
            continue
        if fn in seen_fns:
            continue
        seen_fns.add(fn)
        step_fns.append(fn)
    if len(step_fns) < 2:
        return []

    # Pull the upper-cased source body for each step function from
    # multi_source. Functions not in multi_source are treated as having
    # empty source — the chain link involving them is then unverifiable
    # (return a warning).
    sources = {}
    for fn, data in multi_source.items():
        body = (data or {}).get("source_code") or []
        text_parts = []
        for item in body:
            if isinstance(item, dict):
                text_parts.append(str(item.get("text", "")))
            else:
                text_parts.append(str(item))
        sources[fn.upper()] = " ".join(text_parts).upper()

    # Coarse table-name extraction: the OFSAA convention names tables
    # FCT_*, DIM_*, STG_*, FSI_*, SETUP_*, AAI_*. We pull every such
    # token from each function's source body. A chain link is
    # "supported" iff the two consecutive functions share at least one
    # such table reference.
    table_re = re.compile(
        r"\b(FCT_[A-Z0-9_]+|DIM_[A-Z0-9_]+|STG_[A-Z0-9_]+|"
        r"FSI_[A-Z0-9_]+|SETUP_[A-Z0-9_]+|AAI_[A-Z0-9_]+)\b"
    )
    fn_tables: Dict[str, set] = {
        fn: set(table_re.findall(text)) for fn, text in sources.items()
    }

    warnings: List[str] = []
    for i in range(len(step_fns) - 1):
        a, b = step_fns[i], step_fns[i + 1]
        if a not in fn_tables or b not in fn_tables:
            warnings.append(
                f"GROUNDING-HIGH: response presents '{a}' → '{b}' as "
                f"sequential steps, but at least one is not in "
                f"retrieved sources"
            )
            continue
        shared = fn_tables[a] & fn_tables[b]
        if not shared:
            warnings.append(
                f"GROUNDING-HIGH: response presents '{a}' → '{b}' as "
                f"sequential steps, but their sources share no table "
                f"references (no chain support detected)"
            )
    return warnings


_W57_HIERARCHY_BANNER_RE = re.compile(
    r"This function runs in\s+([^\n→]+?)\s+→",
    re.IGNORECASE,
)


def _w57_check_hierarchy_body_consistency(
    markdown: str,
    multi_source: Dict[str, Any],
    redis_client: Any,
) -> List[str]:
    """W57 Check 4: hierarchy banner schema/batch must match the
    schemas/batches of cited functions.

    The hierarchy banner ("This function runs in {batch} → ...") is
    rendered for the most-relevant retrieved function. If the body of
    the response then discusses functions whose batch differs from the
    banner's batch, the banner and body disagree.

    Implementation: extract the banner batch token, then for each
    function in ``multi_source`` look up its declared batch via Redis.
    If no cited function shares the banner batch, the response is
    inconsistent.
    """
    if redis_client is None:
        return []

    match = _W57_HIERARCHY_BANNER_RE.search(markdown)
    if not match:
        return []
    banner_batch = match.group(1).strip()
    if not banner_batch:
        return []

    cited_batches: set[str] = set()
    try:
        from src.parsing.store import get_function_graph
        from src.parsing.schema_discovery import discovered_schemas
        schemas = discovered_schemas(redis_client)
    except Exception as exc:
        logger.debug("W57 hierarchy/body consistency setup failed: %s", exc)
        return []

    for fn in multi_source:
        for schema in schemas:
            try:
                graph = get_function_graph(redis_client, schema, fn.upper())
            except Exception:
                continue
            if not graph:
                continue
            hierarchy = graph.get("hierarchy") or {}
            batch = hierarchy.get("batch") or ""
            if batch:
                cited_batches.add(batch)
            break  # found in this schema; don't look in others

    if not cited_batches:
        return []  # no batch info anywhere; can't compare
    if banner_batch not in cited_batches:
        return [
            f"GROUNDING-HIGH: hierarchy banner names batch "
            f"'{banner_batch}' but cited functions belong to "
            f"batch(es) {sorted(cited_batches)}; banner and body disagree"
        ]
    return []


def _w57_check_template_phrases(
    markdown: str,
    multi_source: Dict[str, Any],
) -> List[str]:
    """W57 Check 5: detect generic template phrases the model produces
    when it hasn't actually read the source.

    Each phrase has a validator. The phrase being present is a soft
    signal; the validator confirms by checking whether ANY cited source
    actually supports the claim. Mismatch → warning.
    """
    warnings: List[str] = []
    lower = markdown.lower()
    if not multi_source:
        return warnings
    # Concatenate all cited sources for the validator. A template claim
    # is grounded if SOME cited function supports it.
    full_source = _concat_multi_source(multi_source)
    for phrase, validator in _W57_TEMPLATE_PHRASES:
        if phrase not in lower:
            continue
        try:
            supported = validator(full_source)
        except Exception:
            supported = False
        if not supported:
            warnings.append(
                f"GROUNDING-HIGH: response contains template phrase "
                f"'{phrase}' but no cited source supports it"
            )
    return warnings


def _w57_check_caveat_vs_badge(markdown: str) -> List[str]:
    """W57 Check 6: if the system itself emitted a self-aware caveat,
    the badge must reflect that.

    This is the highest-leverage check: RTIE already knows when it's
    confused (NAMED_FUNCTION_NOT_RETRIEVED, partial-source, etc.) and
    appends a "may describe functions related to ..." sanity message.
    Currently that message ships alongside a VERIFIED badge. W57 forces
    UNVERIFIED whenever any caveat trigger appears in the rendered text.
    """
    lower = markdown.lower()
    for trigger in _W57_CAVEAT_TRIGGERS:
        if trigger in lower:
            return [
                f"GROUNDING-HIGH: response contains self-aware caveat "
                f"('{trigger}'); badge auto-flipped to UNVERIFIED"
            ]
    return []


def w57_enforce_grounding(
    raw_query: str,
    markdown: str,
    multi_source: Dict[str, Any],
    functions_analyzed: List[str],
    redis_client: Any = None,
) -> List[str]:
    """Run all six W57 grounding checks. Returns combined warnings list.

    Each warning carries its severity in the prefix:

      * ``GROUNDING-HIGH:`` — content trust violation. Hallucinated
        function references, unbound citation ranges, asked-about-
        function mismatch, fabricated chains, hierarchy/body
        contradictions, unsupported template claims, self-flagged
        caveats. These flip the badge to UNVERIFIED via the badge
        logic in :func:`evaluate_grounding`.
      * ``GROUNDING-LOW:`` — citation hygiene only. Range repeated
        more than the threshold, total citation count above the cap.
        These signal padding but the cited content may still be
        correct, so they surface as an advisory in the TrustBanner
        without flipping the badge.

    An empty return means the response passed every check. Each check
    is independent: their order does not matter, and a failure in one
    does not affect the others.

    Output is deduplicated by exact message text (order-preserving,
    first occurrence wins). The per-claim-binding check otherwise
    emits the same "cited function not in retrieved sources" warning
    once per matching citation pattern, which would render the same
    line 13× in the TrustBanner for one fabrication. Dedup at this
    layer keeps the trust contract intact (each unique problem still
    flips the badge) while presenting a clean list to the user.
    """
    warnings: List[str] = []
    warnings.extend(_w57_check_per_claim_binding(
        markdown, multi_source, functions_analyzed
    ))
    warnings.extend(_w57_check_citation_count_cap(markdown))
    warnings.extend(_w57_check_anchoring(
        raw_query, functions_analyzed, markdown
    ))
    warnings.extend(_w57_check_chain_coherence(markdown, multi_source))
    warnings.extend(_w57_check_hierarchy_body_consistency(
        markdown, multi_source, redis_client
    ))
    warnings.extend(_w57_check_template_phrases(markdown, multi_source))
    warnings.extend(_w57_check_caveat_vs_badge(markdown))

    seen: set[str] = set()
    deduped: List[str] = []
    for w in warnings:
        if w in seen:
            continue
        seen.add(w)
        deduped.append(w)
    return deduped


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
        # Optional Redis client used to look up batch/process/sub-process
        # hierarchy for streamed explanations. Wired post-construction from
        # main.py because the graph Redis client is created later in the
        # lifespan. Absence is non-fatal — the hierarchy header is then
        # simply omitted.
        self._redis_client = None

    def set_redis_client(self, redis_client) -> None:
        """Inject the Redis client used to look up hierarchy metadata."""
        self._redis_client = redis_client

    def hierarchy_header(self, state: LogicState) -> str:
        """Build the one-line hierarchy context header for a response.

        Resolves the primary function (top-ranked ``multi_source`` entry by
        score, falling back to ``object_name``) and fetches its graph from
        Redis. If the graph carries a ``hierarchy`` block, returns a
        prefix string that can be prepended to the explanation; otherwise
        returns an empty string. Also prefixes the inactive-task notice
        when the primary function is marked inactive.

        Phase 3 fix: previously the ranking used ``reverse=True`` over the
        cosine-distance score, which picks the WORST match. That was
        invisible pre-Phase-3 because the index held only OFSMDM; once
        OFSERM joined, the worst match was reliably an OFSERM function
        whose hierarchy was looked up under OFSMDM and missed. The fix
        ranks ASC (lowest score = closest match) and prefers the
        per-result ``schema`` field that Phase 3's
        ``MetadataInterpreter.fetch_multi_logic`` now stamps onto each
        ``multi_source`` entry, falling through to ``state["schema"]``
        only when the entry doesn't carry one.
        """
        if self._redis_client is None:
            return ""

        multi_source = state.get("multi_source") or {}
        primary_fn: str = ""
        primary_schema: str = ""
        if multi_source:
            # COSINE distance: lower is better. Sort ASC so the closest
            # match wins.
            ranked = sorted(
                multi_source.items(),
                key=lambda kv: (kv[1] or {}).get("score", 0) or 0,
            )
            primary_fn = ranked[0][0]
            primary_schema = ((ranked[0][1] or {}).get("schema") or "").strip()
        if not primary_fn:
            primary_fn = (state.get("object_name") or "").strip()
        if not primary_fn:
            return ""

        schema = (
            primary_schema
            or (state.get("schema") or "").strip()
            or fallback_to_default_schema(
                "logic_explainer.hierarchy_header",
                state.get("correlation_id", ""),
            )
        )

        try:
            from src.parsing.store import get_function_graph
            graph = get_function_graph(self._redis_client, schema, primary_fn.upper())
        except Exception as exc:  # Redis miss / serialisation error shouldn't
            logger.debug("hierarchy lookup failed for %s: %s", primary_fn, exc)
            return ""
        if graph is None:
            return ""

        hierarchy = graph.get("hierarchy")
        if not hierarchy:
            return ""

        batch = hierarchy.get("batch") or ""
        process = hierarchy.get("process") or ""
        sub_process = hierarchy.get("sub_process") or ""
        order = hierarchy.get("task_order")
        parts = [p for p in (batch, process, sub_process) if p]
        if not parts:
            return ""

        order_suffix = f" (task #{order})" if isinstance(order, int) else ""
        header = (
            f"This function runs in {' → '.join(parts)}{order_suffix}.\n\n"
        )

        if hierarchy.get("active") is False:
            reason = hierarchy.get("inactive_reason") or "reason not recorded"
            header = (
                "_Note: This task is marked inactive in the current batch "
                f"configuration (reason: {reason}). The explanation below "
                "describes what it would do if it were active._\n\n"
                + header
            )
        return header

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

        try:
            response = await llm.ainvoke(messages)
        except Exception as exc:
            raise sanitize_llm_exception(
                exc, context="explain_logic", correlation_id=correlation_id
            ) from exc

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

        try:
            response = await llm.ainvoke(messages)
        except Exception as exc:
            raise sanitize_llm_exception(
                exc, context="explain_semantic", correlation_id=correlation_id
            ) from exc
        markdown_content = response.content.strip()

        # Prepend hierarchy + derivation context headers when available.
        # Order is hierarchy -> derivation -> body so the deterministic
        # Phase 7 banner sits between the W39 hierarchy line and the
        # LLM-generated step-by-step explanation.
        derivation = render_derivation_header(state)
        header = self.hierarchy_header(state)
        prefix = (header or "") + (derivation or "")
        if prefix:
            markdown_content = prefix + markdown_content

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

        # The hierarchy header is emitted by the caller (main.py's stream
        # endpoint) once before any stream_* call, so that VARIABLE_TRACE
        # queries that bypass this method still get a header. We
        # deliberately do NOT emit it here to avoid duplication.

        try:
            async for chunk in llm.astream(messages):
                if chunk.content:
                    yield chunk.content
        except Exception as exc:
            raise sanitize_llm_exception(
                exc, context="stream_semantic"
            ) from exc

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
