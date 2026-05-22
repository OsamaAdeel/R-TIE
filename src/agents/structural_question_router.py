"""W129 — Structural-question pre-classifier router.

Pre-W129 the LLM classifier over-fired DATA_QUERY on questions about
CODE (functions / batches / jobs / processes / scripts that operate on
data) when the query mentioned date-shaped tokens ("December") or
referenced an ``FCT_*`` table directly. The downstream MIS-date
clarification at :mod:`src.main`'s ``_data_query_stream`` then asked
"include a date" for questions that didn't want a value lookup at all
— a wrong-shape decline that fails the trust contract.

Baseline failures: E1 "What runs only in December?" + E2 "What
functions update FCT_STANDARD_ACCT_HEAD?" of
``scratch/quality_harness_report_baseline.md``.

This module mirrors :mod:`src.agents.computation_router`'s W88 pattern
(static registry → pre-classifier match → stamp ``state["query_type"]``
→ bypass ``classify_query``). The hook lives in :mod:`src.main` as the
``else`` branch of W130's W88 hook, so W88 wins precedence on overlap
(e.g. "what functions compute LCR" — unlikely but should route via W88
decline, not W129 structural).

Pattern shape (deliberately conservative):

  P1: "What/Which <code-noun> <data-op-verb>"
      Code-noun set: functions, code, batches, jobs, tasks, processes,
                     scripts, stored procedures, modules
      Verb set: runs, executes, updates, writes, reads, references,
                feeds, populates
      (``use``/``uses`` was deliberately dropped per W129 design — too
      ambiguous, might want different routing. ``calls`` also out.)

  P2: "What <run-verb> (in|on|during|when) <time-period>"
      Run-verb set: runs, executes, fires

Suggested route on match: ``COLUMN_LOGIC``. The logic_explainer
pipeline handles "find code matching this concept" via semantic search
retrieval; validated by C3's W127 post-fix routing. VARIABLE_TRACE
would require a target_variable W129 queries don't have (C04 / C12
'What writes <COLUMN>?' is the VARIABLE_TRACE shape — left untouched by
W129 since 'what writes' has no intermediate code-noun).

Architecture choice (consistency with W88): static Python module-level
tuple of (compiled regex, route) pairs. Adding a new pattern is a code
change, not a config change — appropriate for content this design-
critical. False-positive risk (re-routing a legitimate DATA_QUERY)
strictly worse than false-negative risk (leaving a structural question
misrouted), so patterns stay narrow.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class W129StructuralMatch:
    """Result of :func:`detect_structural_question`.

    Carries the matched pattern source (for telemetry / logging) and
    the route the caller should stamp into ``state["query_type"]``.
    Frozen so the registry can't be mutated at runtime — patterns are
    declarative.
    """

    pattern: str
    suggested_route: str


# Static registry — (compiled regex, suggested_route) pairs. Order
# does not affect correctness because the patterns are mutually
# disjoint by construction (P1 requires a code-noun between question
# word and verb; P2 requires a preposition after the verb).
_W129_STRUCTURAL_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # P1: "What/Which <code-noun> <data-op-verb>" — E2 case
    # Examples: "What functions update FCT_*", "Which batches write to ...",
    #           "What code populates X"
    (
        re.compile(
            r"\b(what|which)\s+"
            r"(functions?|code|batches?|jobs?|tasks?|processes?|"
            r"scripts?|stored\s+procedures?|modules?)\s+"
            r"(runs?|executes?|updates?|writes?|reads?|"
            r"references?|feeds?|populates?)\b",
            re.IGNORECASE,
        ),
        "COLUMN_LOGIC",
    ),
    # P2: "What <run-verb> (in|on|during|when) <time-period>" — E1 case
    # Examples: "What runs only in December", "What executes during EOM",
    #           "What fires when N_RUN_SKEY changes"
    (
        re.compile(
            r"\bwhat\s+(runs?|executes?|fires?)\s+"
            r"(only\s+)?(in|on|during|when)\b",
            re.IGNORECASE,
        ),
        "COLUMN_LOGIC",
    ),
)


def detect_structural_question(
    raw_query: Optional[str],
) -> Optional[W129StructuralMatch]:
    """Match a query against the structural-question registry.

    Returns the first matching :class:`W129StructuralMatch`, or
    ``None``. Deterministic regex matching only — no LLM, no Redis,
    no Oracle. Pure function — safe to call without backend state.

    Unlike :func:`src.agents.computation_router.detect_named_computation`,
    this function is NOT gated on ``query_type`` because the W129 hook
    in :mod:`src.main` runs *before* the classifier (and bypasses it on
    match). The caller stamps ``state["query_type"] = match.suggested_route``
    explicitly.
    """
    if not raw_query or not isinstance(raw_query, str):
        return None
    if not raw_query.strip():
        return None

    for compiled, route in _W129_STRUCTURAL_PATTERNS:
        m = compiled.search(raw_query)
        if m:
            return W129StructuralMatch(
                pattern=compiled.pattern,
                suggested_route=route,
            )
    return None
