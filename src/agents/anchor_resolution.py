"""W70 — explainer anchor resolution.

Determines the user's primary function via a confidence-tiered cascade
(W76 anchor → clean classifier object_name → BI routing → semantic
top-1) and produces a prompt block prepended to
``SEMANTIC_EXPLANATION_PROMPT`` in
:meth:`logic_explainer.LogicExplainer.stream_semantic` and
:meth:`logic_explainer.LogicExplainer.explain_semantic`.

Goal: prevent the explainer LLM from anchoring its body on the wrong
function within the retrieved set. v2 benchmark Run 7 surfaced two
flavors of anchor drift:

  Flavor 1 — body anchors on a function NOT in retrieval (CAP973
  citing ``REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP``).

  Flavor 2 — body anchors on a real-but-wrong function FROM retrieval
  (the ``OPS_RISK_DATA_POPULATION_CSTM`` query whose body drifted to
  the upstream ``CAP_CONSL_EFFECTIVE_SHAREHOLDING_PERCENT``).

W70 fixes anchor selection given retrieval; it does NOT change what
gets retrieved. For CAP-code queries where retrieval doesn't include
the actual computer function, W70 prevents fabricating a name but
cannot produce the right answer — that's W35 Phase 5-7 territory.

Asymmetric design: false positives (anchoring on the wrong function
despite correct primary identification) are NOT tolerable; the
high-confidence MUST language enforces this. False negatives (overly
rigid output when the primary's source genuinely doesn't answer the
user's question) are tolerable — the "say so explicitly" clause lets
the LLM honestly decline rather than anchor on a sibling.
"""

from typing import Any, Dict, Optional

from src.agents.orchestrator import (
    extract_function_candidates,
    function_exists_in_graph,
)
from src.logger import get_logger
from src.pipeline.state import LogicState

logger = get_logger(__name__, concern="app")


def _is_clean_function_name(s: str) -> bool:
    """True iff *s* is a single PL/SQL-looking identifier and nothing else.

    Distinguishes the orchestrator's clean-anchor ``object_name`` (set
    by ``apply_named_function_anchor`` or ``apply_bi_routing``) from
    the classifier's enriched search blob ("How does FN_X work? Explain
    ..."). Also rejects W58-flagged tokens (table prefixes, alias
    literals, column prefixes, manifest names) by reusing
    :func:`extract_function_candidates`' filtering — those are never
    callable function names.
    """
    if not s:
        return False
    s = s.strip()
    if not s or any(ch.isspace() for ch in s):
        return False
    candidates = extract_function_candidates(s)
    return len(candidates) == 1 and candidates[0] == s


def determine_primary_anchor(state: LogicState) -> Optional[Dict[str, Any]]:
    """Cascade the strongest available anchor signal in *state*.

    Order of evaluation:

      1. ``state["w76_anchor"]["function"]`` — explicit
         ``"In <FunctionName>, ..."`` prefix or alias-literal recovery
         (high confidence). Source is ``"w76_<sub>"`` where ``<sub>``
         is whatever the orchestrator stamped (typically ``prefix`` or
         ``alias_fallback``).

      2. ``state["object_name"]`` when it's a clean function name AND
         BI routing did NOT rewrite it (high confidence — preserved
         for the day the classifier produces clean function names
         directly; today the classifier always emits an enriched
         search blob, so this layer is reached primarily after a
         future classifier change or after w76 promotion which
         layer 1 already handled).

      3. ``state["bi_routing"]["function"]`` — CAP-code or other
         business identifier resolved to a function (medium
         confidence). Distinct from layer 2 because the user named a
         business identifier rather than a function, so the framing
         and confidence differ.

      4. W98 — raw_query function-name scan: when the user mentioned
         a callable PL/SQL function in plain text (``"How does
         <Fn> work?"``) without W76 prefix syntax and without a
         business identifier, and exactly one of those candidates
         survived retrieval into ``state["multi_source"]``, anchor on
         it (high confidence, source ``"raw_query_scan"``). Closes
         the gap W80 v1 opened: pre-W80 the classifier-blob embedding
         pulled the named function to semantic top-1; post-W80 the
         clean-input embedding ranks by body semantics, so the named
         function can land anywhere in the retrieved set. This layer
         is the explicit cascade signal that replaces the implicit
         classifier-side safety net.

      5. Lowest-score entry in ``state["multi_source"]`` — semantic
         top-1 (low confidence). Cosine distance: smaller is closer.

    Returns ``None`` only when none of the five layers can produce a
    candidate (e.g. ``multi_source`` is empty, a DECLINED-shaped
    state). The caller then skips anchor injection and emits the
    existing prompt unchanged.
    """
    w76 = state.get("w76_anchor") or {}
    w76_fn = (w76.get("function") or "").strip()
    if w76_fn:
        sub = w76.get("source") or "prefix"
        return {
            "function": w76_fn,
            "source": f"w76_{sub}",
            "confidence": "high",
        }

    bi = state.get("bi_routing") or {}
    bi_fn = (bi.get("function") or "").strip()

    # Layer 2 is gated on "BI didn't fire". When BI rewrote
    # object_name, the same function is reachable as either a clean
    # object_name (high confidence) or a bi_routing record (medium).
    # The medium framing is the right one — the user typed a business
    # identifier, not a clean function name — so let layer 3 own it.
    obj = (state.get("object_name") or "").strip()
    if not bi_fn and _is_clean_function_name(obj):
        return {
            "function": obj,
            "source": "classifier_object",
            "confidence": "high",
        }

    if bi_fn:
        return {
            "function": bi_fn,
            "source": "bi_routing",
            "confidence": "medium",
        }

    multi_source = state.get("multi_source") or {}

    # Layer 4 (W98) — raw_query function-name scan. Picks the named
    # function for "How does <Fn> work?" / "Explain <Fn>" patterns
    # that escape Layer 1 (no "In <Fn>," prefix) and Layer 3 (no BI
    # code). Validates against multi_source keys rather than the
    # graph: the cascade runs AFTER fetch_multi_logic, so a candidate
    # whose body isn't in multi_source can't be anchored on anyway —
    # promote_anchor_to_front would no-op and the explainer would
    # have no source body to describe. Tying the diagnostic stamp to
    # what retrieval actually surfaced keeps w70_anchor honest.
    #
    # Multi-candidate rule: filter raw_query candidates to those
    # present in multi_source (case-insensitive); fire only on a
    # unique survivor. Zero / multiple survivors fall through to
    # Layer 5 semantic top-1 and let W85 ANCHOR-MISMATCH-HIGH catch
    # any drift.
    raw_query = state.get("raw_query") or ""
    if raw_query and multi_source:
        candidates = extract_function_candidates(raw_query)
        if candidates:
            ms_upper_to_actual = {k.upper(): k for k in multi_source.keys()}
            matched = [
                ms_upper_to_actual[c.upper()]
                for c in candidates
                if c.upper() in ms_upper_to_actual
            ]
            if len(matched) == 1:
                return {
                    "function": matched[0],
                    "source": "raw_query_scan",
                    "confidence": "high",
                }

    if multi_source:
        def _score(item):
            data = item[1]
            if not isinstance(data, dict):
                return float("inf")
            v = data.get("score")
            if v is None:
                return float("inf")
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return float("inf")
            # W149: cosine distance is in [0, 2]; a score outside that range is
            # the W80c "no vector score" sentinel (graph_rerank.NO_VECTOR_SCORE)
            # on a 1-hop-expansion candidate that was never a vector hit.
            # Exclude it so semantic_top1 anchors on a genuine cosine hit, never
            # a graph-expansion neighbour. W95/W147 injected entries use score
            # 0.0 and resolve at L1/L4 BEFORE this layer, so they are unaffected.
            if fv > 2.0:
                return float("inf")
            return fv

        top_item = min(multi_source.items(), key=_score)
        # W149: if even the best candidate has no genuine cosine score (every
        # entry is an expansion/sentinel), there is no semantic_top1 to anchor
        # on — return None so the caller emits no anchor block rather than
        # anchoring on a graph-expansion neighbour.
        if _score(top_item) == float("inf"):
            return None
        return {
            "function": top_item[0],
            "source": "semantic_top1",
            "confidence": "low",
        }

    return None


# ---------------------------------------------------------------------------
# W150 — near-twin disambiguation gate
#
# W149 made L5 select on genuine cosine distance (the 0.0 expansion sentinel is
# excluded). That unmasked a deeper limit: for described-not-named queries in
# dense sibling clusters, the genuinely-closest embedding is frequently a
# near-twin of the asked function (significant vs insignificant, above vs below,
# AT1 vs T1), and the explainer then confidently describes the wrong twin.
#
# W150 does NOT fix retrieval. It is a SAFETY hedge: when the L5 anchor sits in
# a tight near-twin cohort the embedding can't separate, RTIE stops answering
# confidently and emits an UNVERIFIED disambiguation prompt instead.
#
# The margin diagnostic (scratch/findingb_margin_report.md, driver
# tmp_findingb_margin.py) measured that margin ALONE does not separate
# silent-miss from HIT, but (top1≈top2 stem-cohort) AND (margin < 0.05) catches
# 8/10 misses while false-hedging only dense-cluster correct answers (0
# distinctive queries). The cohort predicate below is ported verbatim from that
# driver — it is the version that produced those acceptance numbers; do not
# loosen it without re-running the offline gate (Group-B 0-false-hedge is the
# non-negotiable invariant the stem test guarantees).
# ---------------------------------------------------------------------------

_W150_INVERSIONS = [
    {"SIGNIFICANT", "INSIGNIFICANT"}, {"ABOVE", "BELOW"},
    {"INDIVIDUAL", "AGGREGATE"}, {"AT1", "T1", "T2", "TIER", "CET1", "2"},
    {"AMOUNT", "PERCENTAGE", "PERCENT"}, {"GOODWILL", "OTHER", "INTANGIBLE"},
]
_W150_MARGIN_MAX = 0.05
_W150_COSINE_MAX = 2.0  # RediSearch COSINE ceiling; > this == W149 sentinel
_W150_SIBLINGS = 5      # top-N cohort siblings listed in the hedge


def _w150_tokens(fn: str) -> list:
    return [t for t in (fn or "").upper().split("_") if t]


def _w150_common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(_w150_tokens(a), _w150_tokens(b)):
        if x == y:
            n += 1
        else:
            break
    return n


def _w150_jaccard(a: str, b: str) -> float:
    sa, sb = set(_w150_tokens(a)), set(_w150_tokens(b))
    return len(sa & sb) / len(sa | sb) if (sa | sb) else 0.0


def _w150_near_twin(a: str, b: str) -> bool:
    """True iff two function names form a near-twin cohort: a shared leading
    stem (>=2 tokens) plus either a known one-word inversion swap or high
    token-set overlap. Ported verbatim from tmp_findingb_margin.py — the
    predicate that produced the W150 acceptance numbers."""
    if not a or not b:
        return False
    diff = set(_w150_tokens(a)) ^ set(_w150_tokens(b))
    inv = any(diff & pair for pair in _W150_INVERSIONS)
    return _w150_common_prefix_len(a, b) >= 2 and (
        inv or _w150_jaccard(a, b) >= 0.6
    )


def detect_near_twin_ambiguity(
    state: LogicState,
    anchor: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """W150 — decide whether to hedge instead of confidently anchoring.

    Fires ONLY on the described-not-named L5 path, identified by
    ``anchor["source"] == "semantic_top1"`` — the single signal that excludes
    every resolved path (W76 prefix, classifier_object, bi_routing,
    raw_query_scan), so a NAMED query can never trigger the hedge.

    Returns ``{"top1", "margin", "siblings"}`` when the two closest genuine
    cosine candidates form a near-twin cohort (:func:`_w150_near_twin`) AND
    their distance margin is < ``_W150_MARGIN_MAX``. Returns ``None`` otherwise
    (caller proceeds to the normal confident explainer path).
    """
    if not isinstance(anchor, dict) or anchor.get("source") != "semantic_top1":
        return None

    multi_source = state.get("multi_source") or {}
    real = []
    for fn, data in multi_source.items():
        if not isinstance(data, dict):
            continue
        v = data.get("score")
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv <= _W150_COSINE_MAX:  # exclude the W149 >2.0 expansion sentinel
            real.append((fn, fv))
    real.sort(key=lambda kv: kv[1])

    if len(real) < 2:
        return None
    (top1_fn, top1_d), (top2_fn, top2_d) = real[0], real[1]
    # Defensive re-affirm of condition 2 (a genuine cosine hit won L5). Post-W149
    # source="semantic_top1" already implies this, but never hedge on a sentinel.
    if top1_d > _W150_COSINE_MAX:
        return None
    if not _w150_near_twin(top1_fn, top2_fn):
        return None
    margin = top2_d - top1_d
    if margin >= _W150_MARGIN_MAX:
        return None
    return {
        "top1": top1_fn,
        "margin": margin,
        "siblings": [fn for fn, _ in real[:_W150_SIBLINGS]],
    }


_HIGH_BLOCK = (
    "PRIMARY FUNCTION: {fn}\n"
    "\n"
    "Your explanation MUST describe THIS function. The other functions "
    "in the context are reference material only — describe them ONLY "
    "when they directly explain the behavior of {fn}. If {fn}'s source "
    "doesn't fully answer the user's question, say so explicitly rather "
    "than describing a different function.\n"
    "\n"
)

_MEDIUM_BLOCK = (
    "PRIMARY FUNCTION: {fn}\n"
    "\n"
    "Anchor your explanation on this function. The user asked about a "
    "business identifier (like a CAP code) which RTIE resolved to this "
    "function. Describe what THIS function does for the requested "
    "identifier. The other functions in the context are reference "
    "material.\n"
    "\n"
)

_LOW_BLOCK = (
    "LIKELY PRIMARY FUNCTION: {fn}\n"
    "\n"
    "This function appeared most relevant to the user's query. Anchor "
    "your explanation on it where reasonable. If the user's question "
    "is better answered by a different function in the context, you "
    "may anchor there, but state which function you're describing at "
    "the top of your response.\n"
    "\n"
)


def build_anchor_block(anchor: Optional[Dict[str, Any]]) -> str:
    """Render the anchor block to prepend to ``SEMANTIC_EXPLANATION_PROMPT``.

    Confidence-tiered:

      * ``high`` — mandates the anchor with a "say so explicitly"
        escape hatch when the primary's source is genuinely thin.
      * ``medium`` — frames the anchor as a BI-resolved redirect.
      * ``low`` — softens to "you may anchor elsewhere if the
        question genuinely demands it" with an instruction to name
        the chosen target up front.

    Returns ``""`` for ``None`` input — caller emits the prompt
    unchanged.
    """
    if anchor is None:
        return ""
    fn = anchor["function"]
    conf = anchor["confidence"]
    if conf == "high":
        return _HIGH_BLOCK.format(fn=fn)
    if conf == "medium":
        return _MEDIUM_BLOCK.format(fn=fn)
    return _LOW_BLOCK.format(fn=fn)


def resolve_search_query(state: LogicState) -> str:
    """W80: resolve the input string fed to the vector-search embedding.

    Precedence:

      1. ``state["object_name"]`` when non-empty — this is the clean
         anchor stamped by W76 ``apply_named_function_anchor`` or by
         BI ``apply_bi_routing``. After W80 the classifier no longer
         writes this field, so a non-empty value is always a clean
         function identifier.
      2. ``state["raw_query"]`` — the user's verbatim question. The
         fallback for anchorless queries. Reflects the user's intent
         without classifier restatement noise.
      3. Empty string — defensive only; ``raw_query`` is set at
         endpoint entry so this branch is unreachable in practice.

    The explicit ``or`` form (rather than ``dict.get(key, default)``)
    is load-bearing: ``object_name`` can be present-but-empty (initial
    state seeds it to ``""``), and an empty string must fall through
    rather than be returned as-is.
    """
    return state.get("object_name") or state.get("raw_query") or ""


def ensure_anchor_in_search_results(state: LogicState) -> LogicState:
    """W95 — force the anchor-resolved function into ``search_results``.

    Architectural principle: anchor resolution (W76 / BI routing) must be
    reflected in downstream retrieval, not just embedding bias. Without
    this helper, when vector search ranks the anchored function outside
    the top-K, the source-fetch pipeline never loads its body — the
    explainer is handed sibling functions only and either hallucinates
    (caught post-hoc by W57 as ``GROUNDING-HIGH`` / ``UNVERIFIED``) or
    anchors on a wrong function.

    Sibling fixes that established the same principle at earlier stages:

      * W43 — graph pipeline failing to honour the routed schema.
      * W80 v1 — vector embedding input being the classifier blob
        instead of the clean anchor (``resolve_search_query`` above).

    W95 closes the gap one stage further downstream: between vector
    search and source fetch. The injected record is a sentinel — empty
    metadata fields are fine because
    :meth:`MetadataInterpreter.fetch_multi_logic` resolves the owning
    schema and reads the source body from the parsed graph, keyed off
    ``function_name`` alone.

    Idempotent. No-op when:

      * No anchor signal is present (neither W76 nor BI routing fired).
      * The W76 anchor record exists but its ``function`` is empty
        (alias-literal cleared fallback — see
        ``apply_named_function_anchor`` mechanism 2).
      * The anchored function is already in ``state["search_results"]``
        (case-insensitive match on ``function_name``).

    Source priority — first non-empty wins:

      1. ``state["w76_anchor"]["function"]`` (user explicitly named a
         function via ``"In <Fn>, ..."`` prefix or alias-literal
         recovery).
      2. ``state["bi_routing"]["function"]`` (CAP-code / business
         identifier resolved through the literal index).

    Mutates and returns *state* for chainable callers.
    """
    w76 = state.get("w76_anchor") or {}
    w76_fn = (w76.get("function") or "").strip() if isinstance(w76, dict) else ""

    bi = state.get("bi_routing") or {}
    bi_fn = (bi.get("function") or "").strip() if isinstance(bi, dict) else ""

    anchor_fn = w76_fn or bi_fn
    if not anchor_fn:
        return state

    search_results = list(state.get("search_results") or [])
    anchor_upper = anchor_fn.upper()
    for r in search_results:
        if not isinstance(r, dict):
            continue
        if (r.get("function_name") or "").upper() == anchor_upper:
            return state

    # Schema priority: BI routing's resolved schema (most specific) >
    # request-level state.schema > empty. fetch_multi_logic resolves
    # the actual owning schema per-function from Redis regardless, so
    # an empty value here is safe — this is a hint, not a directive.
    anchor_schema = ""
    if isinstance(bi, dict):
        anchor_schema = (bi.get("schema") or "").strip()
    if not anchor_schema:
        anchor_schema = state.get("schema", "") or ""

    injected = {
        "function_name": anchor_fn,
        "schema": anchor_schema,
        "module": "",
        "description": "",
        "tables_read": "",
        "tables_written": "",
        "key_columns": "",
        "score": 0.0,
        "anchor_injected": True,
    }
    state["search_results"] = [injected] + search_results

    logger.info(
        "ensure_anchor_in_search_results: injected %s (source=%s, schema=%r) "
        "at position 0 — anchored function was missing from %d vector results",
        anchor_fn,
        "w76" if w76_fn else "bi_routing",
        anchor_schema,
        len(search_results),
    )
    return state


def ensure_named_functions_in_search_results(
    state: LogicState,
    redis_client: Any = None,
) -> LogicState:
    """W147 — force plain-text-named functions into ``search_results``.

    Companion to :func:`ensure_anchor_in_search_results` (W95). W95 only
    injects when an *explicit* anchor fired — a W76 ``"In <Fn>, ..."``
    prefix / alias-literal recovery, or BI routing. A query that names a
    function in plain prose ("What feeds data into FN_G_TEST_CSTM?") sets
    neither ``w76_anchor`` nor ``bi_routing``, so when the named function
    lands outside the vector-search top-K its body is never loaded into
    ``multi_source``. The W49 partial-source detector then judges
    body-absence from the (retrieval-derived) ``multi_source`` while
    confirming metadata-presence directly against Redis — an asymmetry
    that falsely reports ``PARTIAL_SOURCE_INDEXED`` even though
    ``graph:source:<schema>:<fn>`` holds the full body (the W147 false
    positive; see ``docs/RTIE_Weakness_Log.md``).

    This helper is the *real* fix: it scans ``raw_query`` for callable
    PL/SQL function names using the SAME W58-gated candidate extractor the
    rest of the pipeline relies on (:func:`extract_function_candidates`),
    and injects any candidate that

      * is verified to exist in graph metadata in some discovered schema
        (:func:`function_exists_in_graph`), AND
      * is not already present in ``search_results``
        (case-insensitive on ``function_name``),

    using the same W95 sentinel shape so
    :meth:`MetadataInterpreter.fetch_multi_logic` resolves the owning
    schema and loads the body. With the body in ``multi_source``, W98's
    raw-query scan (:func:`determine_primary_anchor` layer 4) anchors on
    it and :func:`promote_anchor_to_front` (W97) lifts it to position 0 —
    so the explainer answers the question AND the W49 false positive
    disappears at the source.

    GUARD (non-negotiable): only graph-verified names are injected. A
    candidate that fails the W58 exclusion gates (table / alias / column
    prefix, manifest process name, stopword) is never produced by
    :func:`extract_function_candidates`; a surviving candidate with no
    graph metadata in any schema is skipped. Phantom / unverified names
    are never injected.

    Appends injected records to the END of ``search_results`` rather than
    position 0 so W95's anchor (when it also fired) keeps its primacy;
    W97 owns final ``multi_source`` prominence regardless of search-result
    position. No-op when ``redis_client`` is ``None`` (can't verify graph
    membership → fail closed, never inject unverified) or when the query
    yields no graph-verified candidates.

    Idempotent. Mutates and returns *state* for chainable callers.
    """
    if redis_client is None:
        return state

    raw_query = state.get("raw_query") or ""
    if not raw_query:
        return state

    candidates = extract_function_candidates(raw_query)
    if not candidates:
        return state

    search_results = list(state.get("search_results") or [])
    present_upper = {
        (r.get("function_name") or "").upper()
        for r in search_results
        if isinstance(r, dict)
    }

    injected_names: list = []
    seen_upper = set()
    for candidate in candidates:
        cand_upper = candidate.upper()
        if cand_upper in present_upper or cand_upper in seen_upper:
            continue
        # GUARD: only inject names that genuinely exist in the graph.
        if not function_exists_in_graph(candidate, redis_client):
            continue
        seen_upper.add(cand_upper)
        injected = {
            "function_name": candidate,
            "schema": state.get("schema", "") or "",
            "module": "",
            "description": "",
            "tables_read": "",
            "tables_written": "",
            "key_columns": "",
            "score": 0.0,
            "anchor_injected": True,
        }
        search_results.append(injected)
        injected_names.append(candidate)

    if not injected_names:
        return state

    state["search_results"] = search_results
    logger.info(
        "ensure_named_functions_in_search_results: injected %s — "
        "graph-verified named function(s) were missing from search_results "
        "(W147 retrieval-coverage gap)",
        injected_names,
    )
    return state


def ensure_column_writers_in_search_results(state: LogicState) -> LogicState:
    """Force the column-provenance WRITER set into ``search_results``.

    Companion to :func:`ensure_anchor_in_search_results` (W95, single anchor)
    and :func:`ensure_named_functions_in_search_results` (W147, plain-prose
    names). Where W95 force-injects exactly one anchor at position 0, a column
    can have *several* writers (e.g. ``N_EOP_BAL`` ←
    ``POPULATE_PP_FROMGL`` AND ``POPULATE_PP_FROMGL_AMC``); the writer/INSERT-
    aware trace path must see ALL of them in ``multi_source`` or it will
    narrate an incomplete provenance. This helper injects every writer the
    column-provenance anchor resolved (:func:`apply_column_provenance_anchor`)
    that is not already present.

    Writers are appended to the END of ``search_results`` (the W147 contract)
    rather than position 0: VARIABLE_TRACE has no single primary anchor — the
    tracer walks the whole retrieved set — and appending avoids fighting W95's
    position-0 injection when both fire on the same turn. W89 reorders the
    resulting ``multi_source`` by manifest execution order downstream.

    The writers were already resolved and verified against the structured
    graph by the anchor pass, so no Redis lookup is needed here — this is a
    pure state rewrite. No-op when ``state["column_provenance"]`` is absent or
    carries no writers (mirrors the Redis-unavailable / no-op contract of its
    siblings). Idempotent. Mutates and returns *state*.
    """
    provenance = state.get("column_provenance") or {}
    if not isinstance(provenance, dict):
        return state
    writers = provenance.get("writers") or []
    if not writers:
        return state

    search_results = list(state.get("search_results") or [])
    present_upper = {
        (r.get("function_name") or "").upper()
        for r in search_results
        if isinstance(r, dict)
    }

    injected_names: list = []
    seen_upper: set = set()
    for writer in writers:
        if not isinstance(writer, dict):
            continue
        fn = (writer.get("function") or "").strip()
        if not fn:
            continue
        fn_upper = fn.upper()
        if fn_upper in present_upper or fn_upper in seen_upper:
            continue
        seen_upper.add(fn_upper)
        search_results.append({
            "function_name": fn,
            "schema": writer.get("schema", "") or "",
            "module": "",
            "description": "",
            "tables_read": "",
            "tables_written": "",
            "key_columns": "",
            "score": 0.0,
            "anchor_injected": True,
        })
        injected_names.append(fn)

    if not injected_names:
        return state

    state["search_results"] = search_results
    logger.info(
        "ensure_column_writers_in_search_results: injected %s — column "
        "writer(s) for %r were missing from search_results",
        injected_names, provenance.get("column", ""),
    )
    return state


def apply_w70_anchor(state: LogicState) -> Optional[Dict[str, Any]]:
    """Compute primary anchor, stamp diagnostic onto *state*, log decision.

    Mirrors the W76 stamp pattern: writes ``state["w70_anchor"]`` (which
    may be ``None``) so downstream diagnostics, tests, and operator
    introspection can see what the explainer was told to anchor on.

    Returns the anchor dict for the caller to feed into
    :func:`build_anchor_block`. The caller is responsible for
    prepending the rendered block to its system prompt.
    """
    anchor = determine_primary_anchor(state)
    state["w70_anchor"] = anchor  # type: ignore[typeddict-item]
    if anchor:
        logger.info(
            "apply_w70_anchor: anchored on %s (source=%s, confidence=%s)",
            anchor["function"], anchor["source"], anchor["confidence"],
        )
    else:
        logger.info("apply_w70_anchor: no anchor available")
    return anchor


def promote_anchor_to_front(
    multi_source: Dict[str, Any],
    anchor: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """W97 — promote the anchored function to ``multi_source`` position 0.

    Architectural principle: anchor resolution must dominate both
    retrieval coverage AND prompt prominence. W95
    (:func:`ensure_anchor_in_search_results`) closed the coverage gap —
    when the anchored function was missing from ``search_results`` it
    is force-injected at index 0. W97 closes the prominence gap — when
    the anchored function IS in ``multi_source`` but at a low rank
    (e.g. position 30 of 35 after the W80c-v2 widened retrieval
    window), the explainer LLM reads 30 sibling function sections
    before reaching the anchored body and over-weights whichever
    sibling occupies position 0. Promoting the anchor to position 0
    of ``multi_source`` places its source first in the user-message
    function pile, reinforcing the system-prompt anchor block with
    primacy-of-appearance.

    W80c-v2 canary surfaced the failure: the FN_LOAD_OPS_RISK_DATA
    query landed UNVERIFIED with both ``GROUNDING-HIGH`` and
    ``GROUNDING-ANCHOR-MISMATCH-HIGH`` despite the W70 high-confidence
    anchor block (``"PRIMARY FUNCTION: FN_LOAD_OPS_RISK_DATA — your
    explanation MUST describe THIS function ..."``). The directive sits
    ~3500 tokens earlier in the system message while a wall of 30
    sibling function bodies dominates the user message — the LLM
    drifted to the position-0 retrieval entry
    (``PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP``).

    Idempotent. No-op when:

      * ``anchor`` is ``None`` or has an empty ``function``.
      * ``multi_source`` is empty.
      * The anchored function is not in ``multi_source``
        (case-insensitive match on the key). The companion W95 helper
        runs upstream of ``fetch_multi_logic`` so the anchor IS
        normally present by the time W97 fires — but a no-op here is
        the safe behaviour when retrieval and anchor disagree.
      * The anchored function is already at position 0 (the W95
        injection path).

    Preserves the relative order of all non-anchor entries. The
    returned dict is fresh — the input is not mutated. Python's
    insertion-order semantics (3.7+) carry through to downstream
    callers that iterate ``multi_source.items()`` or
    ``list(multi_source.keys())``.

    Runs AFTER :func:`chain_ordering.reorder_multi_source` (W89) in
    :mod:`src.main` so anchor-first wins when the manifest task_order
    chain and the user's explicit anchor disagree — answering the
    function the user asked about beats showing the chain in execution
    order.
    """
    if not multi_source:
        return multi_source
    if not isinstance(anchor, dict):
        return multi_source
    anchor_fn = (anchor.get("function") or "").strip()
    if not anchor_fn:
        return multi_source

    anchor_upper = anchor_fn.upper()
    keys = list(multi_source.keys())
    # Case-insensitive lookup so we tolerate any casing mismatch
    # between the anchor cascade and the search_results key.
    upper_to_actual = {k.upper(): k for k in keys}
    actual_key = upper_to_actual.get(anchor_upper)
    if actual_key is None:
        return multi_source
    if keys[0].upper() == anchor_upper:
        return multi_source

    promoted = {actual_key: multi_source[actual_key]}
    for k in keys:
        if k == actual_key:
            continue
        promoted[k] = multi_source[k]

    logger.info(
        "promote_anchor_to_front: moved %s from position %d to position 0 "
        "(multi_source size=%d)",
        actual_key, keys.index(actual_key), len(keys),
    )
    return promoted
