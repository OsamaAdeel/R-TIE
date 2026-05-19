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

from src.agents.orchestrator import extract_function_candidates
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

      4. Lowest-score entry in ``state["multi_source"]`` — semantic
         top-1 (low confidence). Cosine distance: smaller is closer.

    Returns ``None`` only when none of the four layers can produce a
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
    if multi_source:
        def _score(item):
            data = item[1]
            if not isinstance(data, dict):
                return float("inf")
            v = data.get("score")
            if v is None:
                return float("inf")
            try:
                return float(v)
            except (TypeError, ValueError):
                return float("inf")

        top_fn = min(multi_source.items(), key=_score)[0]
        return {
            "function": top_fn,
            "source": "semantic_top1",
            "confidence": "low",
        }

    return None


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
