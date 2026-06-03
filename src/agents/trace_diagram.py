"""
RTIE Trace Diagram Assembler — W151 Phase 1.

Pure, side-effect-free assembler that projects an already-resolved trace into
the diagram ``{nodes, edges, groups}`` grammar
(``docs/trace_diagram_grammar_spec.md`` §1 / §3.2), stamping **per-element
grounding** as a *projection* of what RTIE already knows at assembly time
(Option A from the per-element grounding decision note).

It is deliberately inert:

* makes no LLM call,
* performs no Redis read and opens no new source-resolution path,
* emits no SSE event and does not touch ``main.py``,
* does NOT modify ``evaluate_grounding`` or any W57/W83/W85 check.

The caller (a later wiring phase) gathers the inputs — ``multi_source`` and the
``grounding`` verdict from pipeline state, the fan-in steps from the value/graph
producers, and the derivation records from the derivation index — and passes
them in. This keeps the assembler a pure function over explicit inputs (the
proof chain and derivation records are *not* in ``LogicState``).

Grounding rules baked in here, verbatim from the decision note:

1. **NODE** — VERIFIED iff (function ∈ ``multi_source``) AND the citation has a
   real resolved ``[start, end]`` span actually present in that function's
   ``source_code``. Else UNVERIFIED.
2. **EDGE** — an edge is a claim about *flow*, not an endpoint. VERIFIED iff
   (its function ∈ ``multi_source`` AND a real resolved span) AND it is NOT a
   member of an ``alternative`` group AND ``ungroundedGap`` is false.
   Alternatives and gaps are ALWAYS UNVERIFIED regardless of citation.
3. **BODY BADGE AS RENDER CEILING** — after computing each element's intrinsic
   grounding, if ``grounding["badge"] != "VERIFIED"`` every VERIFIED element is
   downgraded to UNVERIFIED. So ``diagram_grounding == grounding["badge"]`` by
   construction; gaps/alternatives stay dashed even when the body is VERIFIED.
4. **AGGREGATE** — ``diagram_grounding = grounding["badge"]`` (by construction
   from rule 3). On a ``DECLINED`` badge the assembler returns ``None`` — DECLINED
   is a separate response path that never reaches here; the guard is explicit.

Citation atom (closes W51): every node/edge citation
``{function, lines, text, grounding}`` is sliced from the SAME
``multi_source[fn]["source_code"]`` list — line numbers AND text from one
resolve, never regex-scraped from rendered markdown.
"""

from typing import Any, Dict, List, Optional

VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
DECLINED = "DECLINED"

# Bound an embedded citation excerpt to this many source lines. Overflow is
# served by a later-phase /v1/source endpoint; for now we cap and mark the
# citation truncated.
TEXT_LINE_CAP = 80

# node_type / operation → node kind (caller-supplied "kind" always wins).
_NODE_KIND_BY_OP = {
    "INSERT": "derived-column",
    "UPDATE": "derived-column",
    "MERGE": "derived-column",
    "ASSIGN": "derived-column",
    "SELECT": "source-table",
    "READ": "source-table",
    "SOURCE": "source-table",
    "TABLE": "source-table",
    "FILTER": "filter",
    "WHERE": "filter",
}

# node_type / operation → edge kind (caller-supplied "edge_kind" always wins).
_EDGE_KIND_BY_OP = {
    "INSERT": "writes",
    "UPDATE": "writes",
    "MERGE": "writes",
    "ASSIGN": "writes",
    "SELECT": "reads",
    "READ": "reads",
    "SOURCE": "reads",
}

__all__ = ["build_trace_diagram", "diagram_from_bi_routing"]


# ---------------------------------------------------------------------------
# Citation atom — sliced from multi_source, NEVER from markdown.
# ---------------------------------------------------------------------------
def _norm_lines(lines: Optional[List[int]]) -> List[int]:
    """Coerce a lines value into a ``[start, end]`` int pair; ``[0, 0]`` when
    absent or malformed (``[0, 0]`` denotes "no resolved span")."""
    if not lines or len(lines) < 2:
        return [0, 0]
    try:
        start, end = int(lines[0]), int(lines[1])
    except (TypeError, ValueError):
        return [0, 0]
    return [start, end]


def _resolve_citation(
    function: str,
    lines: Optional[List[int]],
    multi_source: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the citation atom by slicing ``multi_source[function]`` source.

    Returns ``{function, lines, text[, truncated]}`` (grounding is stamped by
    the caller after the per-element rule runs). ``has_real_span`` — whether the
    slice actually matched at least one source line in the resolved body — is
    returned alongside so the grounding rule can use it.
    """
    start, end = _norm_lines(lines)
    entry = multi_source.get(function) if function else None

    matched: List[Dict[str, Any]] = []
    if entry:
        for line in entry.get("source_code") or []:
            if not isinstance(line, dict):
                continue
            ln = line.get("line")
            if isinstance(ln, int) and start <= ln <= end:
                matched.append(line)

    truncated = False
    if len(matched) > TEXT_LINE_CAP:
        matched = matched[:TEXT_LINE_CAP]
        truncated = True

    text = "\n".join(str(m.get("text", "")).rstrip("\n") for m in matched)

    # "real resolved span" per rules 1 & 2: a sane [start,end] that actually
    # landed on >= 1 line of the resolved source_code. A [0,0] / unresolved /
    # not-in-multi_source span is NOT real.
    has_real_span = bool(matched) and start > 0 and end >= start

    citation: Dict[str, Any] = {
        "function": function,
        "lines": [start, end],
        "text": text,
    }
    if truncated:
        citation["truncated"] = True
    return citation, has_real_span


def _node_grounding(function: str, has_real_span: bool, multi_source: Dict[str, Any]) -> str:
    """Rule 1: VERIFIED iff function ∈ multi_source AND a real resolved span."""
    in_ms = bool(function) and function in multi_source
    return VERIFIED if (in_ms and has_real_span) else UNVERIFIED


def _edge_grounding(
    function: str,
    has_real_span: bool,
    multi_source: Dict[str, Any],
    in_alternative: bool,
    ungrounded_gap: bool,
) -> str:
    """Rule 2: alternatives and gaps are ALWAYS UNVERIFIED; otherwise same as a
    node (member + real span)."""
    if in_alternative or ungrounded_gap:
        return UNVERIFIED
    in_ms = bool(function) and function in multi_source
    return VERIFIED if (in_ms and has_real_span) else UNVERIFIED


def _schema_of(function: str, multi_source: Dict[str, Any]) -> str:
    entry = multi_source.get(function) if function else None
    return (entry or {}).get("schema", "") if entry else ""


# ---------------------------------------------------------------------------
# Shape mapper: derivation DAG  (e.g. CAP943 = CAP309 - CAP863)
# ---------------------------------------------------------------------------
def _assemble_derivation_dag(
    target: str,
    derivation_records: List[Dict[str, Any]],
    multi_source: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """Map derivation records (derivations.py:678-689) into nodes + edges.

    target_literal → target node; each source_literal/operand → node; a
    SUBTRACT operation → two edges into the target (minuend "+", subtrahend
    "−"); DIRECT_ASSIGN → one edge ("="). Every element cites the record's
    single ``{function, line_range}``.
    """
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    seen_nodes = set()

    # Prefer the record(s) whose target matches `target`; fall back to all.
    records = [r for r in derivation_records if r.get("target_literal") == target]
    if not records:
        records = list(derivation_records)

    for rec in records:
        function = rec.get("function", "")
        line_range = rec.get("line_range") or [0, 0]
        target_lit = rec.get("target_literal", target)
        operation = (rec.get("operation") or "").upper()
        operands = rec.get("operands") or [
            {"literal": lit} for lit in (rec.get("source_literals") or [])
        ]

        # target node
        if target_lit not in seen_nodes:
            cit, real = _resolve_citation(function, line_range, multi_source)
            cit["grounding"] = _node_grounding(function, real, multi_source)
            nodes.append({
                "id": target_lit,
                "label": target_lit,
                "kind": "cap-literal",
                "schema": _schema_of(function, multi_source),
                "citation": cit,
            })
            seen_nodes.add(target_lit)

        for idx, op in enumerate(operands):
            lit = op.get("literal") or f"operand_{idx}"
            if lit not in seen_nodes:
                cit, real = _resolve_citation(function, line_range, multi_source)
                cit["grounding"] = _node_grounding(function, real, multi_source)
                nodes.append({
                    "id": lit,
                    "label": lit,
                    "kind": "cap-literal",
                    "schema": _schema_of(function, multi_source),
                    "citation": cit,
                })
                seen_nodes.add(lit)

            # sign label: SUBTRACT → first operand minuend (+), rest subtrahend (−)
            if operation == "SUBTRACT":
                sign = "+" if idx == 0 else "−"
                edge_kind = "subtract-operand"
            else:
                sign = "="
                edge_kind = "derives"

            cit, real = _resolve_citation(function, line_range, multi_source)
            grounding = _edge_grounding(
                function, real, multi_source,
                in_alternative=False, ungrounded_gap=False,
            )
            cit["grounding"] = grounding
            edges.append({
                "id": f"e_{lit}_{target_lit}",
                "from": lit,
                "to": target_lit,
                "kind": edge_kind,
                "label": sign,
                "grounding": grounding,
                "ungroundedGap": False,
                "citation": cit,
            })

    return {"nodes": nodes, "edges": edges, "groups": []}


# ---------------------------------------------------------------------------
# Shape mapper: column fan-in  (e.g. fan-in → N_STD_ACCT_HEAD_AMT)
# ---------------------------------------------------------------------------
def _kind_for_step(step: Dict[str, Any]) -> str:
    if step.get("kind"):
        return step["kind"]
    op = (step.get("node_type") or step.get("operation") or "").upper()
    return _NODE_KIND_BY_OP.get(op, "intermediate")


def _edge_kind_for_step(step: Dict[str, Any]) -> str:
    if step.get("edge_kind"):
        return step["edge_kind"]
    op = (step.get("node_type") or step.get("operation") or "").upper()
    return _EDGE_KIND_BY_OP.get(op, "writes")


def _assemble_fan_in(
    target: str,
    fan_in_steps: List[Dict[str, Any]],
    alternatives: Optional[List[Dict[str, Any]]],
    multi_source: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """Map structured fan-in steps (per-function graph nodes / proof steps,
    carrying ``node_id, function, node_type/operation, line_start, line_end``)
    into nodes + edges converging on the ``target`` column.

    Each step becomes a node; each step's outgoing edge flows to its declared
    ``successor`` or, by default, to the target sink. A step in an alternative
    group (``alternatives[*].members``) yields an UNVERIFIED edge (rule 2); a
    step marked ``ungrounded_gap`` yields a dashed ``ungroundedGap`` edge.

    Note: the target sink node is UNVERIFIED unless a step explicitly defines it
    (``node_id == target``). Per rule 1 the column being traced *to* carries no
    span of its own — its trust comes from the writer edge, not from itself.
    """
    groups = _normalize_groups(alternatives)
    alt_members = set()
    divergence_nodes = set()
    for g in groups:
        alt_members.update(g.get("members") or [])
        between = (g.get("divergence") or {}).get("between") or []
        divergence_nodes.update(between)

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    node_ids = set()

    def _add_node(node_id, label, kind, function, lines, schema):
        if node_id in node_ids:
            return
        cit, real = _resolve_citation(function, lines, multi_source)
        cit["grounding"] = _node_grounding(function, real, multi_source)
        node: Dict[str, Any] = {
            "id": node_id,
            "label": label or node_id,
            "kind": kind,
            "schema": schema,
            "citation": cit,
        }
        if node_id in divergence_nodes:
            node["isDivergence"] = True
        nodes.append(node)
        node_ids.add(node_id)

    target_defined = any(s.get("node_id") == target for s in fan_in_steps)

    for step in fan_in_steps:
        node_id = step.get("node_id")
        if not node_id:
            continue
        function = step.get("function", "")
        lines = [step.get("line_start", 0), step.get("line_end", 0)]
        _add_node(
            node_id,
            step.get("label"),
            "target-column" if node_id == target else _kind_for_step(step),
            function,
            lines,
            _schema_of(function, multi_source),
        )

        # Outgoing edge: to declared successor, else to the target sink.
        successor = step.get("successor") or target
        if successor == node_id:
            continue  # self-loop guard

        ungrounded_gap = bool(step.get("ungrounded_gap"))
        in_alt = (node_id in alt_members) or (successor in alt_members)

        cit, real = _resolve_citation(function, lines, multi_source)
        grounding = _edge_grounding(
            function, real, multi_source,
            in_alternative=in_alt, ungrounded_gap=ungrounded_gap,
        )
        cit["grounding"] = grounding
        edges.append({
            "id": f"e_{node_id}_{successor}",
            "from": node_id,
            "to": successor,
            "kind": step.get("edge_kind") or (
                "candidate-writes" if in_alt else _edge_kind_for_step(step)
            ),
            "grounding": grounding,
            "ungroundedGap": ungrounded_gap,
            "citation": cit,
        })

    # Ensure the target sink node exists even if no step defined it — but only
    # when real source nodes were produced. With no steps there is no trace, so
    # we leave nodes empty and let the caller-facing None guard fire.
    if node_ids and not target_defined and target not in node_ids:
        cit, _ = _resolve_citation("", [0, 0], multi_source)
        cit["grounding"] = UNVERIFIED
        nodes.append({
            "id": target,
            "label": target,
            "kind": "target-column",
            "schema": "",
            "citation": cit,
        })

    return {"nodes": nodes, "edges": edges, "groups": groups}


def _normalize_groups(
    alternatives: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Pass alternative groups through with a stable, fully-keyed shape."""
    out: List[Dict[str, Any]] = []
    for g in alternatives or []:
        out.append({
            "kind": g.get("kind", "alternative"),
            "label": g.get("label", ""),
            "members": list(g.get("members") or []),
            "candidates": list(g.get("candidates") or []),
            "divergence": dict(g.get("divergence") or {}),
        })
    return out


# ---------------------------------------------------------------------------
# Ceiling (rule 3) — downgrade everything when the body badge is not VERIFIED.
# ---------------------------------------------------------------------------
def _apply_ceiling(diagram: Dict[str, Any], badge: str) -> None:
    """In-place downgrade: when the body badge is not VERIFIED, no element may
    render solid. Stamps both the element-level and citation-level grounding so
    the (dumb) renderer never has to reconcile two values."""
    if badge == VERIFIED:
        return
    for node in diagram["nodes"]:
        node["citation"]["grounding"] = UNVERIFIED
    for edge in diagram["edges"]:
        edge["grounding"] = UNVERIFIED
        edge["citation"]["grounding"] = UNVERIFIED


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_trace_diagram(
    *,
    target: str,
    trace_kind: str,
    multi_source: Dict[str, Any],
    grounding: Dict[str, Any],
    fan_in_steps: Optional[List[Dict[str, Any]]] = None,
    derivation_records: Optional[List[Dict[str, Any]]] = None,
    alternatives: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Assemble a trace diagram payload, or ``None`` for DECLINED / non-trace.

    Args:
        target: the column or CAP-literal the trace converges on.
        trace_kind: ``"fan-in"`` or ``"derivation-dag"``.
        multi_source: ``state["multi_source"]`` — the resolved source cohort.
            ``functions_analyzed == list(multi_source.keys())`` (main.py:1777),
            i.e. the same cohort the body badge consumed.
        grounding: the ``evaluate_grounding`` result; ``grounding["badge"]`` is
            the render ceiling (rule 3) and the diagram aggregate (rule 4).
        fan_in_steps: structured fan-in nodes (graph nodes / proof steps).
        derivation_records: derivation index records for the DAG shape.
        alternatives: optional ``alternative`` group records.

    Returns:
        ``{target, trace_kind, diagram_grounding, nodes, edges, groups}`` with
        per-element grounding stamped, or ``None`` when the badge is DECLINED,
        the trace_kind is unknown, or no nodes could be assembled.
    """
    badge = (grounding or {}).get("badge", UNVERIFIED)

    # Rule 4 guard: DECLINED is a separate response path; emit no diagram.
    if badge == DECLINED:
        return None

    multi_source = multi_source or {}

    if trace_kind == "derivation-dag":
        assembled = _assemble_derivation_dag(
            target, derivation_records or [], multi_source
        )
    elif trace_kind == "fan-in":
        assembled = _assemble_fan_in(
            target, fan_in_steps or [], alternatives, multi_source
        )
    else:
        # Non-trace / unknown shape — no diagram.
        return None

    if not assembled["nodes"]:
        return None

    diagram: Dict[str, Any] = {
        "target": target,
        "trace_kind": trace_kind,
        # Rule 4: aggregate == body badge, made true by construction below.
        "diagram_grounding": badge,
        "nodes": assembled["nodes"],
        "edges": assembled["edges"],
        "groups": assembled["groups"],
    }

    # Rule 3: body badge as render ceiling.
    _apply_ceiling(diagram, badge)

    return diagram


# ---------------------------------------------------------------------------
# Stream orchestration (W151 Phase 3) — derivation-dag from BI routing.
# ---------------------------------------------------------------------------
def diagram_from_bi_routing(
    bi_routing: Optional[Dict[str, Any]],
    multi_source: Dict[str, Any],
    grounding: Dict[str, Any],
    graph_lookup,
) -> Optional[Dict[str, Any]]:
    """Build a derivation-dag diagram for a BI-routed CAP query, or ``None``.

    Pure orchestration over :func:`build_trace_diagram`. The full derivation
    records (operands + line_range) live on the per-function graph at
    ``graph["derivations"]`` (loader.py:448-449), NOT in ``state`` and NOT in
    the literal-index summary carried on ``bi_routing["derivation"]``. The
    caller injects ``graph_lookup`` — a callable ``(schema, function) ->
    graph_dict | None`` (e.g. ``get_function_graph``) — so this stays
    Redis-free and unit-testable.

    Returns ``None`` when there is no BI routing, the routing record is
    incomplete, the resolved function has no derivation records, or the
    assembler declines (e.g. DECLINED badge). The caller should treat any
    exception from ``graph_lookup`` as "no diagram" — a diagram must never
    break the stream.
    """
    if not bi_routing:
        return None
    identifier = bi_routing.get("identifier")
    function = bi_routing.get("function")
    schema = bi_routing.get("schema")
    if not (identifier and function and schema):
        return None

    graph = graph_lookup(schema, function) or {}
    records = graph.get("derivations") or []
    if not records:
        return None

    return build_trace_diagram(
        target=identifier,
        trace_kind="derivation-dag",
        multi_source=multi_source or {},
        grounding=grounding or {},
        derivation_records=records,
    )
