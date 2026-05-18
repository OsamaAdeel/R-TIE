"""W88 — Named regulatory computation pre-router for DATA_QUERY.

When a DATA_QUERY query names a Basel-defined regulatory computation
("BIA op risk capital", "CET1 ratio", "RWA for credit risk"), the
classifier correctly routes as DATA_QUERY but the SQL generator has
no anchor to the canonical OFSERM fact table or filter shape. Result:
fabricated SQL against OFSMDM staging, often returning null but
badged VERIFIED — a W57-class trust gap surfaced by W86's all-null
metric catch but not prevented at the routing layer.

W88 inserts a deterministic pre-router between :func:`_resolve_target_schema`
and :func:`_build_schema_catalog` in :mod:`src.agents.data_query`. When
the query matches a registered named computation, the router short-
circuits LLM SQL generation with hand-built canonical SQL against the
known fact table + filter, then lets the existing Guardian + execute
path run unchanged. When no match, it's a no-op and today's path runs.

Two arms (v1 = 9 items, 6 anchor + 3 decline; diagnostic Section 5):

**Anchor arm (6 items)** — canonical SQL emitted against the known
fact table with the known filter. Reachable in the current Oracle.

- ``BIA``  — ``OFSERM.FCT_OPS_RISK_DATA`` filtered to
  ``N_BASEL_METHOD_SKEY`` for code ``ORBIA``; SUM(N_CAPITAL_CHARGE).
- ``CREDIT_RWA_AGG`` — ``OFSERM.FCT_STANDARD_ACCT_HEAD`` at
  ``V_STD_ACCT_HEAD_ID='CAP169'``; aggregate Credit RWA (no
  methodology split — that requires FCT_NON_SEC_EXPOSURES which is
  not present locally; see diagnostic Section 2 item 5).
- ``MARKET_RWA_AGG`` — same fact table at ``CAP090``; aggregate
  Market RWA.
- ``CET1`` — same fact table at ``CAP960``; Common Equity Tier 1
  capital ratio.
- ``TIER1`` — same fact table at ``CAP214``; Tier 1 capital ratio.
- ``CAR`` — same fact table at ``CAP192``; Total Capital ratio.

**Decline arm (3 items)** — no SQL emitted; honest UNVERIFIED-style
payload explaining why the computation isn't answerable. Distinct
from W45 / W49 declines (those are FUNCTION_LOGIC — function name
not in graph) — W88 declines reference regulatory concepts and
mention what loaded data IS available as an alternative.

- ``LEVERAGE_RATIO`` — placeholder at ``CAP843`` reads 0.0; no
  loader function populates it.
- ``LCR`` — no fact table containing LCR exists in either OFSERM or
  OFSMDM; lives in the OFSAA Liquidity Risk Management module which
  is not loaded here.
- ``NSFR`` — same as LCR.

**SKEY resolution** for method-discriminated anchors (BIA in v1):
the method SKEY (e.g. ORBIA → 115) is resolved at first use via a
``DIM_BASEL_METHODOLOGY`` lookup and cached in module-global
:data:`_SKEY_CACHE`. On lookup failure (Oracle unreachable; OFSAA
upgrade has different codes; DIM not populated), the affected
anchor degrades to a decline-arm response — never ship a guessed
SKEY value. Cap-code anchors don't need this resolution because
the CAP code itself is the stable filter.

**Architecture choice** (D5): static Python dict. Inventory is
small (15 items in the diagnostic; 9 implemented in v1), Basel +
OFSAA seed codes are stable across deployments, and LLM fallback
is exactly today's failure mode. Adding new items is a code change
not a config change — appropriate for content this stable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# Static registry. Each entry declares the surface (patterns), the arm
# (anchor or decline), and arm-specific fields.

@dataclass(frozen=True)
class W88ComputationDefinition:
    """Static definition of a recognized named computation.

    Frozen so the registry can't be mutated at runtime. SKEY resolution
    happens via :func:`resolve_skey_for_anchor`, not by mutating the
    definition — the registry stays purely declarative.
    """

    name: str
    long_name: str
    arm: str  # "anchor" | "decline"
    patterns: tuple[str, ...]
    # Anchor-arm fields. Unused for decline arm.
    target_schema: Optional[str] = None
    target_table: Optional[str] = None
    result_column: Optional[str] = None
    filter_kind: str = "none"  # "method_skey" | "cap_code" | "none"
    filter_code: Optional[str] = None  # ORBIA / CAP160 / etc.
    aggregate_fn: Optional[str] = None  # "SUM" | None (single-row select)
    notes: str = ""
    # Decline-arm fields. Unused for anchor arm.
    decline_reason: Optional[str] = None
    decline_alternative: Optional[str] = None


W88_NAMED_COMPUTATIONS: tuple[W88ComputationDefinition, ...] = (
    # -------------------------------------------------------------------
    # Anchor arm — 6 fully-reachable items per diagnostic Section 5
    # -------------------------------------------------------------------
    W88ComputationDefinition(
        name="BIA",
        long_name="Basic Indicator Approach",
        arm="anchor",
        patterns=(
            r"\bbasic[\s\-_]+indicator[\s\-_]+approach\b",
            r"\bbasic[\s\-_]+indicator\b",
            r"\bBIA\b",
        ),
        target_schema="OFSERM",
        target_table="FCT_OPS_RISK_DATA",
        result_column="N_CAPITAL_CHARGE",
        filter_kind="method_skey",
        filter_code="ORBIA",
        aggregate_fn="SUM",
        notes=(
            "SUM(N_CAPITAL_CHARGE) over the latest (N_MIS_DATE_SKEY, "
            "N_RUN_SKEY) for method ORBIA; aggregates across entities. "
            "For RWA the column is N_RWA_AMT (= N_CAPITAL_CHARGE * 12.5)."
        ),
    ),
    W88ComputationDefinition(
        name="CREDIT_RWA_AGG",
        long_name="Credit Risk RWA (aggregate)",
        arm="anchor",
        patterns=(
            r"\bcredit[\s\-_]+risk[\s\-_]+rwa\b",
            r"\bcredit[\s\-_]+rwa\b",
            r"\brwa[\s\-_]+(for[\s\-_]+)?credit[\s\-_]+risk\b",
            r"\bCAP169\b",
        ),
        target_schema="OFSERM",
        target_table="FCT_STANDARD_ACCT_HEAD",
        result_column="N_STD_ACCT_HEAD_AMT",
        filter_kind="cap_code",
        filter_code="CAP169",
        aggregate_fn=None,
        notes=(
            "Aggregate Credit RWA — single row per (run, date). Methodology "
            "breakdown not loaded (FCT_NON_SEC_EXPOSURES absent)."
        ),
    ),
    W88ComputationDefinition(
        name="MARKET_RWA_AGG",
        long_name="Market Risk RWA (aggregate)",
        arm="anchor",
        patterns=(
            r"\bmarket[\s\-_]+risk[\s\-_]+rwa\b",
            r"\bmarket[\s\-_]+rwa\b",
            r"\brwa[\s\-_]+(for[\s\-_]+)?market[\s\-_]+risk\b",
            r"\bCAP090\b",
        ),
        target_schema="OFSERM",
        target_table="FCT_STANDARD_ACCT_HEAD",
        result_column="N_STD_ACCT_HEAD_AMT",
        filter_kind="cap_code",
        filter_code="CAP090",
        aggregate_fn=None,
        notes=(
            "Aggregate Market RWA — populated via GL path "
            "(MKT_RISK_RWA_STD_ACCT_HEAD_DATA_POP); per-methodology "
            "breakdown via FCT_MARKET_RISK_SUMMARY is empty in this run."
        ),
    ),
    W88ComputationDefinition(
        name="CET1",
        long_name="Common Equity Tier 1 capital ratio",
        arm="anchor",
        patterns=(
            r"\bCET[\s\-_]*1\b",
            r"\bcommon[\s\-_]+equity[\s\-_]+tier[\s\-_]+1\b",
            r"\bCAP960\b",
        ),
        target_schema="OFSERM",
        target_table="FCT_STANDARD_ACCT_HEAD",
        result_column="N_STD_ACCT_HEAD_AMT",
        filter_kind="cap_code",
        filter_code="CAP960",
        aggregate_fn=None,
        notes=(
            "CET1 ratio = CAP841 (Net CET1 Capital) / CAP838 (Total RWA), "
            "stored as the ratio at CAP960."
        ),
    ),
    W88ComputationDefinition(
        name="TIER1",
        long_name="Tier 1 capital ratio",
        arm="anchor",
        patterns=(
            r"\btier[\s\-_]*1[\s\-_]+(capital[\s\-_]+)?ratio\b",
            r"\bT1[\s\-_]+(capital[\s\-_]+)?ratio\b",
            r"\bCAP214\b",
        ),
        target_schema="OFSERM",
        target_table="FCT_STANDARD_ACCT_HEAD",
        result_column="N_STD_ACCT_HEAD_AMT",
        filter_kind="cap_code",
        filter_code="CAP214",
        aggregate_fn=None,
        notes=(
            "Equals CET1 in current run because Net Additional Tier 1 "
            "(CAP908) = 0 — the bank holds no AT1 instruments."
        ),
    ),
    W88ComputationDefinition(
        name="CAR",
        long_name="Total Capital ratio (Capital Adequacy Ratio)",
        arm="anchor",
        patterns=(
            r"\btotal[\s\-_]+capital[\s\-_]+ratio\b",
            r"\bcapital[\s\-_]+adequacy[\s\-_]+ratio\b",
            r"\bCAP192\b",
        ),
        target_schema="OFSERM",
        target_table="FCT_STANDARD_ACCT_HEAD",
        result_column="N_STD_ACCT_HEAD_AMT",
        filter_kind="cap_code",
        filter_code="CAP192",
        aggregate_fn=None,
        notes=(
            "CAR = CAP210 (Total Eligible Capital) / CAP838 (Total RWA), "
            "stored as the ratio at CAP192."
        ),
    ),
    # -------------------------------------------------------------------
    # Decline arm — 3 items not answerable from local Oracle
    # -------------------------------------------------------------------
    W88ComputationDefinition(
        name="LEVERAGE_RATIO",
        long_name="Leverage Ratio",
        arm="decline",
        patterns=(
            r"\bleverage[\s\-_]+ratio\b",
            r"\bCAP843\b",
        ),
        decline_reason=(
            "The Leverage Ratio is not computed in the current OFSAA run. "
            "The placeholder slot at OFSERM.FCT_STANDARD_ACCT_HEAD CAP843 "
            "reads 0.0, and no loader function populates it. Reporting "
            "0.0 as the Leverage Ratio would be a fabrication."
        ),
        decline_alternative=(
            "For a leverage-style metric that IS computed in this run, "
            "ask for the Tier 1 Capital Ratio (CAP214)."
        ),
    ),
    W88ComputationDefinition(
        name="LCR",
        long_name="Liquidity Coverage Ratio",
        arm="decline",
        patterns=(
            r"\bliquidity[\s\-_]+coverage[\s\-_]+ratio\b",
            r"\bLCR\b",
        ),
        decline_reason=(
            "The Liquidity Coverage Ratio (LCR) is not part of the OFSAA "
            "module loaded in this RTIE deployment. The loaded module is "
            "Capital Adequacy (ABL_CAR_CSTM_V4); LCR lives in the OFSAA "
            "Liquidity Risk Management module, which is not deployed "
            "here. No fact table containing LCR data exists in either "
            "OFSERM or OFSMDM."
        ),
        decline_alternative=None,
    ),
    W88ComputationDefinition(
        name="NSFR",
        long_name="Net Stable Funding Ratio",
        arm="decline",
        patterns=(
            r"\bnet[\s\-_]+stable[\s\-_]+funding[\s\-_]+ratio\b",
            r"\bNSFR\b",
        ),
        decline_reason=(
            "The Net Stable Funding Ratio (NSFR) is not part of the OFSAA "
            "module loaded in this RTIE deployment. No fact table "
            "containing NSFR exists in either OFSERM or OFSMDM. The only "
            "NSFR-related schema artefact is a residual-maturity band "
            "column on FCT_MITIGANTS, which is not the NSFR result."
        ),
        decline_alternative=None,
    ),
)


# Compiled patterns at import time. Returned in registry order so the
# first declaration's first pattern wins on overlap — deterministic.
def _build_pattern_index():
    out = []
    for defn in W88_NAMED_COMPUTATIONS:
        for pat in defn.patterns:
            out.append((re.compile(pat, re.IGNORECASE), defn))
    return tuple(out)


_PATTERN_INDEX: tuple = _build_pattern_index()


# Module-global SKEY cache. code-string (e.g. "ORBIA") -> SKEY or None.
# Populated lazily by :func:`resolve_skey_for_anchor`; survives across
# requests within a process. Use :func:`reset_skey_cache` in tests.
_SKEY_CACHE: dict[str, Optional[int]] = {}


def reset_skey_cache() -> None:
    """Clear the SKEY cache. Called from tests; not used in production."""
    _SKEY_CACHE.clear()


@dataclass(frozen=True)
class W88AnchorMatch:
    """Result of :func:`detect_named_computation`.

    Carries the matched definition plus the pattern that triggered it
    (for telemetry / logging).
    """

    definition: W88ComputationDefinition
    matched_pattern: str


def detect_named_computation(
    raw_query: Optional[str],
    query_type: Optional[str],
) -> Optional[W88AnchorMatch]:
    """Match a query against the named-computation registry.

    Returns the first matching :class:`W88AnchorMatch`, or ``None``.
    Deterministic regex matching only — no LLM, no Redis, no Oracle.
    Pure function — safe to call without backend state.

    Gates on ``query_type == "DATA_QUERY"`` because other types have
    their own anchor paths (W76 for FUNCTION_LOGIC, BI routing for
    VARIABLE_TRACE). A FUNCTION_LOGIC query like "How is BIA
    calculated?" should hit the existing function-explainer path, not
    W88 — the user is asking about logic, not for a number.
    """
    if query_type != "DATA_QUERY":
        return None
    if not raw_query or not isinstance(raw_query, str):
        return None
    if not raw_query.strip():
        return None

    for compiled, defn in _PATTERN_INDEX:
        m = compiled.search(raw_query)
        if m:
            return W88AnchorMatch(definition=defn, matched_pattern=compiled.pattern)
    return None


async def resolve_skey_for_anchor(
    anchor: W88AnchorMatch,
    schema_tools,
) -> Optional[int]:
    """Resolve method-code → SKEY via ``DIM_BASEL_METHODOLOGY``.

    Cached in :data:`_SKEY_CACHE` keyed by code-string. On lookup
    failure (Oracle unreachable, code not present, exception),
    caches ``None`` so subsequent calls return ``None`` immediately
    without re-probing Oracle until :func:`reset_skey_cache` runs.

    Returns:
        The integer SKEY when ``filter_kind == "method_skey"`` and the
        lookup succeeds.
        ``None`` when the lookup failed (caller should degrade to
        decline arm).
        ``None`` when ``filter_kind != "method_skey"`` (CAP-code anchors
        don't need this resolution).
    """
    defn = anchor.definition
    if defn.filter_kind != "method_skey":
        return None
    code = defn.filter_code
    if code in _SKEY_CACHE:
        return _SKEY_CACHE[code]
    sql = (
        "SELECT N_BASEL_METHOD_SKEY "
        "FROM OFSERM.DIM_BASEL_METHODOLOGY "
        "WHERE V_BASEL_METHOD_CODE = :code "
        "AND F_LATEST_RECORD_INDICATOR = 'Y'"
    )
    try:
        rows = await schema_tools.execute_raw(sql, {"code": code})
        if rows and rows[0] and rows[0][0] is not None:
            skey = int(rows[0][0])
            _SKEY_CACHE[code] = skey
            logger.info(
                "W88 SKEY resolved | code=%s skey=%d", code, skey
            )
            return skey
        logger.warning(
            "W88 SKEY resolution | code=%s not present in "
            "DIM_BASEL_METHODOLOGY (F_LATEST_RECORD_INDICATOR='Y')",
            code,
        )
        _SKEY_CACHE[code] = None
        return None
    except Exception as exc:
        logger.warning(
            "W88 SKEY resolution failed | code=%s error=%s", code, exc
        )
        _SKEY_CACHE[code] = None
        return None


def build_anchor_plan(
    anchor: W88AnchorMatch,
    skey: Optional[int],
) -> Optional[dict]:
    """Build a SQL execution plan for an anchor arm.

    Returns a dict in the same shape :meth:`DataQueryAgent._generate_sql`
    produces (``sql``, ``params``, ``query_kind``, ``count_sql``,
    ``select_columns``) so the existing Guardian + execute path consumes
    it unchanged. Plus a ``w88_anchor`` block carrying routing metadata
    that flows through to the done payload for canaries.

    Returns ``None`` for ``method_skey`` anchors when ``skey is None`` —
    caller must emit a decline payload instead.
    """
    defn = anchor.definition
    if defn.filter_kind == "method_skey":
        if skey is None:
            return None
        return _build_method_skey_plan(defn, skey)
    if defn.filter_kind == "cap_code":
        return _build_cap_code_plan(defn)
    return None


def _build_method_skey_plan(defn: W88ComputationDefinition, skey: int) -> dict:
    """SQL plan for FCT_OPS_RISK_DATA-style anchors.

    Scopes to the latest (N_MIS_DATE_SKEY, N_RUN_SKEY) within the
    method-skey filter via DENSE_RANK, then aggregates across entities.
    Necessary because the table holds multiple runs / dates / entities
    per method (57 rows for ORBIA in current run, spread across 3 runs).
    A naive SUM across all rows gives 408.93 B; the stakeholder-meaningful
    answer for "what is the BIA capital charge?" is the latest run's
    total (20.39 B per cowork). DENSE_RANK selects all rows tied for
    most-recent (date, run); SUM aggregates entities within that run.
    """
    column_alias = defn.name
    sql = (
        f"SELECT {defn.aggregate_fn}(N_CAPITAL_CHARGE) AS {column_alias} "
        "FROM ("
        "  SELECT f.N_CAPITAL_CHARGE, "
        "         DENSE_RANK() OVER ("
        "           ORDER BY f.N_MIS_DATE_SKEY DESC, f.N_RUN_SKEY DESC"
        "         ) AS w88_rk "
        f"  FROM {defn.target_schema}.{defn.target_table} f "
        "  WHERE f.N_BASEL_METHOD_SKEY = :w88_skey"
        ") WHERE w88_rk = 1"
    )
    return {
        "sql": sql,
        "params": {"w88_skey": skey},
        "query_kind": "AGGREGATE",
        "count_sql": None,
        "select_columns": [column_alias],
        "w88_anchor": {
            "name": defn.name,
            "long_name": defn.long_name,
            "target_table": f"{defn.target_schema}.{defn.target_table}",
            "filter": (
                f"N_BASEL_METHOD_SKEY={skey} (code={defn.filter_code}), "
                "latest (date, run)"
            ),
            "result_column": defn.result_column,
        },
    }


def _build_cap_code_plan(defn: W88ComputationDefinition) -> dict:
    """SQL plan for FCT_STANDARD_ACCT_HEAD-style anchors (CAP-code).

    One row per (CAP-code, run, date, entity, GAAP). Picks the latest
    (date, run) via DENSE_RANK and emits a single value. ROWNUM = 1
    narrows to a single entity/GAAP within that run (defensive against
    multi-entity portfolios where ratios might differ; for v1 the
    current run is single-entity and this is a no-op).
    """
    column_alias = defn.name
    sql = (
        f"SELECT {defn.result_column} AS {column_alias} "
        "FROM ("
        f"  SELECT f.{defn.result_column}, "
        "         DENSE_RANK() OVER ("
        "           ORDER BY f.N_MIS_DATE_SKEY DESC, f.N_RUN_SKEY DESC"
        "         ) AS w88_rk "
        f"  FROM {defn.target_schema}.{defn.target_table} f "
        f"  JOIN {defn.target_schema}.DIM_STANDARD_ACCT_HEAD d "
        "    ON f.N_STD_ACCT_HEAD_SKEY = d.N_STD_ACCT_HEAD_SKEY "
        "  WHERE d.V_STD_ACCT_HEAD_ID = :w88_cap_code"
        ") WHERE w88_rk = 1 AND ROWNUM = 1"
    )
    return {
        "sql": sql,
        "params": {"w88_cap_code": defn.filter_code},
        "query_kind": "AGGREGATE",
        "count_sql": None,
        "select_columns": [column_alias],
        "w88_anchor": {
            "name": defn.name,
            "long_name": defn.long_name,
            "target_table": f"{defn.target_schema}.{defn.target_table}",
            "filter": (
                f"V_STD_ACCT_HEAD_ID='{defn.filter_code}', latest (date, run)"
            ),
            "result_column": defn.result_column,
        },
    }


def build_decline_payload(
    anchor: W88AnchorMatch,
    user_query: str,
    correlation_id: str,
    skey_unresolved: bool = False,
) -> dict:
    """Build the honest-decline payload.

    Distinct from W45 / W49 declines (those are FUNCTION_LOGIC, anchored
    on a function name). W88 declines anchor on the regulatory concept
    and explain *why* the computation isn't answerable from the loaded
    data — referencing OFSAA modules, fact tables, or run state.

    Two paths produce a decline:
      1. Pure decline arm (``LEVERAGE_RATIO`` / ``LCR`` / ``NSFR``) —
         the computation has no fact-table path in this deployment.
      2. SKEY-unresolved fallback (anchor arm that couldn't resolve
         its method code at runtime) — surfaces as an operational
         decline so the user knows the registry is intact but the
         catalog probe failed.

    Shape matches the existing ``unsupported`` payload at
    :meth:`DataQueryAgent.answer_stream` line 374-394 (status,
    query_kind, sql, params, rows, columns, row_count, summary,
    explanation, sanity_warnings, verification_sql, correlation_id)
    plus a ``w88_decline`` block for canary assertions.
    """
    defn = anchor.definition
    if skey_unresolved:
        reason = (
            f"The W88 pre-router for {defn.long_name} requires a methodology "
            "SKEY lookup against OFSERM.DIM_BASEL_METHODOLOGY (looking for "
            f"V_BASEL_METHOD_CODE='{defn.filter_code}', "
            "F_LATEST_RECORD_INDICATOR='Y'). That lookup did not return a "
            "SKEY in the current Oracle catalog. RTIE will not guess a "
            "SKEY value because doing so would silently filter on the "
            "wrong methodology. Likely causes: OFSAA upgrade renamed the "
            "code, the methodology dimension is not populated, or the "
            "F_LATEST_RECORD_INDICATOR flag is unset."
        )
        suggestion = None
    else:
        reason = defn.decline_reason or "Computation not answerable in this deployment."
        suggestion = defn.decline_alternative

    md_lines = [
        f"## {defn.long_name} — Not answerable from this deployment",
        "",
        reason,
    ]
    if suggestion:
        md_lines.extend(["", f"**Alternative:** {suggestion}"])
    explanation = "\n".join(md_lines)

    # Note on `badge`: data_query's main.py wrapper derives the badge
    # from `status` (see src/main.py:2061-2071). status="unsupported"
    # maps to "REJECTED" — which reads correctly for W88 declines
    # ("informed refusal to attempt"). We deliberately don't ship a
    # `badge` key here so main.py stays the single authority.
    return {
        "status": "unsupported",
        "query_kind": None,
        "sql": None,
        "count_sql": None,
        "params": {},
        "rows": [],
        "columns": [],
        "row_count": 0,
        "summary": reason,
        "explanation": explanation,
        "sanity_warnings": [f"W88 decline: {defn.name}"],
        "verification_sql": None,
        "correlation_id": correlation_id,
        "w88_decline": {
            "name": defn.name,
            "long_name": defn.long_name,
            "skey_unresolved": skey_unresolved,
        },
    }
