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

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
DECLINED = "DECLINED"

# Bound an embedded citation excerpt to this many source lines. Overflow is
# served by a later-phase /v1/source endpoint; for now we cap and mark the
# citation truncated.
TEXT_LINE_CAP = 80

# W162 Tier 1: a single OFSAA megaline (~5k chars on ONE physical line) never
# trips TEXT_LINE_CAP (it is 1 line), so without a character bound the whole
# statement dumps inline in the citation panel. Cap the embedded excerpt by
# characters too — ~2,000 chars is ample for a normal multi-line excerpt yet
# well under a megaline — so the citation is marked truncated and the existing
# "Load full cited range" affordance (W151 Phase 5) engages instead. Both caps
# apply: truncate if lines > TEXT_LINE_CAP OR chars > TEXT_CHAR_CAP. Presentation
# only — does NOT touch has_real_span / grounding / which lines the citation
# points at.
TEXT_CHAR_CAP = 2000

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

__all__ = [
    "build_trace_diagram",
    "diagram_from_bi_routing",
    "fan_in_steps_from_tagged_lines",
    "fan_in_steps_from_graph",
]

# Variable-trace operation classes (from variable_tracer._classify_operation).
# Writers actually write the target variable; everything else is read/context
# (TRANSFORM = target on the RHS of some other assignment; PARAMETER = IN/OUT
# param) and never asserts a write to the sink.
_WRITER_OPS = {"ASSIGN", "SELECT_INTO", "INSERT", "UPDATE", "MERGE"}
_FANIN_KIND = {
    "ASSIGN": "derived-column",
    "SELECT_INTO": "derived-column",
    "INSERT": "derived-column",
    "UPDATE": "derived-column",
    "MERGE": "derived-column",
    "READ": "source-table",
    "FILTER": "filter",
    "TRANSFORM": "intermediate",
    "PARAMETER": "intermediate",
}


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

    # "real resolved span" per rules 1 & 2: a sane [start,end] that actually
    # landed on >= 1 line of the resolved source_code. A [0,0] / unresolved /
    # not-in-multi_source span is NOT real. Computed BEFORE any cap so a
    # truncated excerpt never weakens grounding — the span is real regardless
    # of how much excerpt text we choose to embed.
    has_real_span = bool(matched) and start > 0 and end >= start

    truncated = False
    if len(matched) > TEXT_LINE_CAP:
        matched = matched[:TEXT_LINE_CAP]
        truncated = True

    text = "\n".join(str(m.get("text", "")).rstrip("\n") for m in matched)

    # W162 Tier 1: a megaline is one physical line that the line cap can't bound.
    # Cap the embedded excerpt by characters and mark it truncated so the
    # "Load full cited range" path engages instead of dumping the wall inline.
    if len(text) > TEXT_CHAR_CAP:
        text = text[:TEXT_CHAR_CAP]
        truncated = True

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

    def _add_node(node_id, label, kind, function, lines, schema, expression=None):
        if node_id in node_ids:
            return
        cit, real = _resolve_citation(function, lines, multi_source)
        cit["grounding"] = _node_grounding(function, real, multi_source)
        # W162 Tier 2a: attach the per-column expression as a display enrichment
        # AFTER grounding is stamped. It is NOT routed through _resolve_citation
        # and NEVER influences has_real_span / grounding — text/lines/grounding
        # stay pointed at the real megaline line; this is an additive view.
        if expression:
            cit["expression"] = expression
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
            expression=step.get("target_expression"),
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


# ---------------------------------------------------------------------------
# Variable-trace fan-in projection (W151 Phase 3.5) — Model A, flat.
# ---------------------------------------------------------------------------
def fan_in_steps_from_tagged_lines(
    tagged_lines: List[Dict[str, Any]],
    target_variable: str,
    *,
    gap: int = 2,
) -> List[Dict[str, Any]]:
    """Project variable-trace ``tagged_lines`` into ``fan_in_steps``.

    Model A (flat, locally-grounded flow only — no cross-function chaining):

      * a **writer** line (ASSIGN/SELECT_INTO/INSERT/UPDATE/MERGE) → the target
        sink  (edge kind ``writes``);
      * a **read/filter** line → its OWN function's **first writer** by line
        (edge kind ``reads``);
      * read/filter lines in a function that has **no writer** are DROPPED — no
        locally-grounded edge exists for them;
      * ``COMMENTED_OUT`` lines are excluded; TRANSFORM/PARAMETER are context,
        never writers.

    Multi-line coalescing: consecutive same-``(function, operation)`` tags whose
    line difference is ``<= gap`` collapse into one node spanning
    ``[line_start, line_end]`` (so a multi-line statement is one node, but two
    distinct statements stay apart).

    Input entry shape (``variable_tracer.extract_relevant_lines``):
    ``{function, line, text, aliases_matched, operation, commented}``.

    Returns step dicts for ``build_trace_diagram(trace_kind="fan-in")``:
    ``{node_id, function, kind, line_start, line_end, label, successor,
    edge_kind}``. Returns ``[]`` when there is no target or no active lines.
    """
    target_variable = (target_variable or "").strip()
    if not target_variable:
        return []

    active = [
        t for t in (tagged_lines or [])
        if not t.get("commented")
        and (t.get("operation") or "").upper() != "COMMENTED_OUT"
    ]
    if not active:
        return []

    # 1. group by (function, operation); split into line-contiguous runs (gap);
    #    coalesce each run into one node.
    by_fn_op: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for t in active:
        op = (t.get("operation") or "").upper()
        by_fn_op[(t.get("function", ""), op)].append(t)

    nodes: List[Dict[str, Any]] = []
    for (fn, op), tags in by_fn_op.items():
        tags.sort(key=lambda t: t.get("line", 0))
        run: List[Dict[str, Any]] = [tags[0]]
        runs: List[List[Dict[str, Any]]] = []
        for prev, cur in zip(tags, tags[1:]):
            if cur.get("line", 0) - prev.get("line", 0) <= gap:
                run.append(cur)
            else:
                runs.append(run)
                run = [cur]
        runs.append(run)

        for r in runs:
            ls = min(t.get("line", 0) for t in r)
            le = max(t.get("line", 0) for t in r)
            is_writer = op in _WRITER_OPS
            label = fn if is_writer else ((r[0].get("text") or fn)[:60])
            nodes.append({
                "node_id": f"{fn}:{op}:L{ls}",
                "function": fn,
                "kind": _FANIN_KIND.get(op, "intermediate"),
                "line_start": ls,
                "line_end": le,
                "label": label,
                "is_writer": is_writer,
            })

    # 2. first writer (by line) per function — the read-attachment target (7.1).
    first_writer: Dict[str, str] = {}
    for n in sorted(nodes, key=lambda n: n["line_start"]):
        if n["is_writer"] and n["function"] not in first_writer:
            first_writer[n["function"]] = n["node_id"]

    # 3. assign successors per Model A; drop writer-less-function reads (7.2).
    steps: List[Dict[str, Any]] = []
    for n in nodes:
        if n["is_writer"]:
            successor, edge_kind = target_variable, "writes"
        else:
            fw = first_writer.get(n["function"])
            if not fw:
                continue
            successor, edge_kind = fw, "reads"
        steps.append({
            "node_id": n["node_id"],
            "function": n["function"],
            "kind": n["kind"],
            "line_start": n["line_start"],
            "line_end": n["line_end"],
            "label": n["label"],
            "successor": successor,
            "edge_kind": edge_kind,
        })
    return steps


# ---------------------------------------------------------------------------
# Common-case graph fan-in projection (W151 Phase 3.6) — Model A, flat.
# ---------------------------------------------------------------------------
# Per-function graph node ``type`` (builder.py) → fan-in node kind for the
# READ side. Writers always render "derived-column". A node is only a *read*
# here when it does NOT structurally write the target (see _node_writes_column).
_GRAPH_READ_KIND = {
    "SCALAR_COMPUTE": "intermediate",
    "INSERT": "source-table",
    "UPDATE": "source-table",
    "MERGE": "source-table",
}


def _column_in_maps(column_maps: Any, target_upper: str) -> bool:
    """True iff *target_upper* appears as a WRITTEN column in a parsed
    ``column_maps`` record — an INSERT ``mapping`` key or an UPDATE
    ``assignments`` left-hand column. A column appearing only as a value /
    RHS expression / WHERE reference is NOT a write and returns False."""
    if not isinstance(column_maps, dict):
        return False
    mapping = column_maps.get("mapping")
    if isinstance(mapping, dict):
        for col in mapping:
            if str(col).strip().upper() == target_upper:
                return True
    for pair in (column_maps.get("assignments") or []):
        try:
            col = pair[0]
        except (TypeError, IndexError, KeyError):
            continue
        if str(col).strip().upper() == target_upper:
            return True
    return False


def _node_writes_column(node: Dict[str, Any], target_upper: str) -> bool:
    """Structural write-attestation (the W153 guard).

    A graph node WRITES the target column ONLY when the column literally
    appears as a *written* target in that node's own parsed records — NOT when
    it is merely mentioned in a filter/condition, on a read, or on an
    expression RHS. The mention-based column index (indexer.py) and the
    cross-function ``matching_columns`` walk (query_engine.py) pull a node into
    the resolved set if it *references* the column at all; this re-derives the
    write structurally so the G-Test / C04 wrong-family failure (W153) cannot
    leak a fabricated writer into the diagram as an authoritative arrow.

    Attestation by node ``type`` (builder.py):

      * ``SCALAR_COMPUTE``      — ``output_variable`` equals the target.
      * ``INSERT`` / ``UPDATE`` — target in the node's ``column_maps``.
      * ``MERGE``               — target in the top-level ``column_maps`` OR in
        either the ``when_matched`` / ``when_not_matched`` arm's ``column_maps``
        (either arm ⇒ one writer node; arms are not split into an alternative
        group).
      * everything else (DELETE, loops, calc sub-types) — never a writer.
    """
    ntype = (node.get("type") or "").upper()
    if ntype == "SCALAR_COMPUTE":
        return (node.get("output_variable") or "").strip().upper() == target_upper
    if ntype in ("INSERT", "UPDATE", "MERGE"):
        if _column_in_maps(node.get("column_maps"), target_upper):
            return True
        if ntype == "MERGE":
            for arm in ("when_matched", "when_not_matched"):
                if _column_in_maps((node.get(arm) or {}).get("column_maps"), target_upper):
                    return True
        return False
    return False


def _expressions_for_column(node: Dict[str, Any], target_upper: str) -> List[Dict[str, Any]]:
    """W162 Tier 2a — the per-column RHS expression(s) a write node assigns to
    *target_upper*, for DISPLAY ENRICHMENT only.

    Mirrors the :func:`_column_in_maps` / :func:`_node_writes_column` traversal
    (INSERT ``mapping[col]``; UPDATE/MERGE ``assignments [(col, expr)]``; MERGE
    ``when_matched`` / ``when_not_matched`` arm maps) but returns the RHS
    expression text rather than a bool. Returns a list of
    ``{"column", "expression", "arms"}`` — identical expressions appearing under
    more than one arm are merged into one entry with the arms collected (so a
    standard MERGE whose USING projection and WHEN-MATCHED SET carry the same
    expression shows ONCE, while genuinely different arm expressions surface
    separately, arm-labeled). Empty list when the node assigns no expression to
    the column (e.g. SCALAR_COMPUTE, or a column written only structurally).

    TRUST: never consulted by grounding. The caller copies the result onto the
    citation AFTER the grounding stamp; it never flows through _resolve_citation,
    has_real_span, or _node_grounding/_edge_grounding.
    """
    raw: List[tuple] = []  # (column, expression, arm)

    def _scan(cm: Any, arm: str) -> None:
        if not isinstance(cm, dict):
            return
        mapping = cm.get("mapping")
        if isinstance(mapping, dict):
            for col, val in mapping.items():
                if str(col).strip().upper() == target_upper:
                    raw.append((str(col), str(val), arm))
        for pair in (cm.get("assignments") or []):
            try:
                col, expr = pair[0], pair[1]
            except (TypeError, IndexError, KeyError):
                continue
            if str(col).strip().upper() == target_upper:
                raw.append((str(col), str(expr), arm))

    _scan(node.get("column_maps"), "main")
    if (node.get("type") or "").upper() == "MERGE":
        for arm in ("when_matched", "when_not_matched"):
            _scan((node.get(arm) or {}).get("column_maps"), arm)

    # Merge identical (column, expression) across arms; preserve first-seen order.
    merged: Dict[tuple, List[str]] = {}
    order: List[tuple] = []
    for col, expr, arm in raw:
        key = (col, expr)
        if key not in merged:
            merged[key] = []
            order.append(key)
        if arm not in merged[key]:
            merged[key].append(arm)
    return [{"column": c, "expression": e, "arms": merged[(c, e)]} for (c, e) in order]


def fan_in_steps_from_graph(
    fetched_nodes: List[Dict[str, Any]],
    target_column: str,
    multi_source: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Project common-path graph nodes into ``fan_in_steps`` (Model A, flat).

    This is the common-case fan-in source (W151 Phase 3.6): graph-resolvable
    VARIABLE_TRACE queries stream via the ``llm_payload`` branch and never set
    the Phase-3.5 ``tagged_lines``, so the fan-in must come from the structured
    per-function graph nodes already fetched on that path
    (``fetch_nodes_by_ids`` — entries shaped
    ``{"function", "node", "execution_condition"[, "is_upstream"]}``).

    Model A (flat, locally-grounded flow only — NO cross-function inference):

      * a node that **structurally writes** the target (``_node_writes_column``)
        → the target sink (edge kind ``writes``);
      * a node that mentions the target but does **not** write it (a read /
        filter / RHS reference) → its OWN function's **first writer** by line
        (edge kind ``reads``);
      * reads in a function with **no** attested writer are DROPPED (7.2) — no
        locally-grounded edge exists, and a wholly-wrong function (W153) thus
        contributes nothing at all;
      * graph **edges are NOT converted** into diagram edges — the merged
        graph's ``matching_columns`` links are exactly the cross-function
        (Model B) inference Model A forbids. Diagram edges are synthesized from
        node write/read attestation only.

    **Cohort scope (prose-alignment invariant).** ``fetched_nodes`` is resolved
    from the *global* column index — every function that writes the column
    across the whole schema — but the prose answer is anchored on the retrieved
    cohort (``functions_analyzed == list(multi_source.keys())``). Drawing the
    global writer set produces a diagram that disagrees with the prose (a common
    column has dozens of global writers) and breaks the W151 core invariant
    (diagram is a navigation aid on the *authoritative prose*). When
    *multi_source* is provided, a candidate writer/read whose function is NOT in
    that cohort is **dropped** (counted as ``scoped_out``), bounding the fan-in
    to exactly the analyzed cohort. When *multi_source* is ``None`` no cohort
    filter applies (used to unit-test the attestation / Model-A logic in
    isolation). This is the prose-alignment fix, not a degree cap.

    Span discipline (the W153 ceiling, applied early): an in-cohort attested
    writer with no resolved ``[line_start, line_end]`` span is **dropped before
    assembly** (not drawn dashed) — an undrawable writer is not a claim. The
    drop count is returned so the fan-in canary can surface coverage loss rather
    than have it silently truncate the diagram. Span-less reads are skipped as
    context.

    Coalescing (3.5's gap rule) is NOT applied: each per-function graph node is
    already one statement-level block with its own span, so there is nothing to
    coalesce.

    Returns ``{"steps", "writer_drops", "writers_total", "scoped_out"}`` —
    ``steps`` for :func:`build_trace_diagram` (``trace_kind="fan-in"``);
    ``writers_total`` is all globally-attested writers, ``scoped_out`` those
    dropped for being outside the cohort, ``writer_drops`` the in-cohort writers
    dropped for a missing span. ``steps`` is empty when there is no target or no
    attested in-cohort writer survives.
    """
    target_upper = (target_column or "").strip().upper()
    if not target_upper:
        return {"steps": [], "writer_drops": 0, "writers_total": 0,
                "scoped_out": 0}

    # Cohort = the analyzed functions the body badge consumed (case-folded).
    # None ⇒ no cohort filter (isolation tests of the topology logic).
    cohort = None
    if multi_source is not None:
        cohort = {str(k).strip().upper() for k in multi_source}

    writer_nodes: List[Dict[str, Any]] = []
    read_nodes: List[Dict[str, Any]] = []
    writer_drops = 0
    writers_total = 0
    scoped_out = 0
    seen_ids: set = set()

    for entry in fetched_nodes or []:
        if not isinstance(entry, dict):
            continue
        node = entry.get("node", entry)
        if not isinstance(node, dict):
            continue
        fn = entry.get("function", "") or ""
        raw_id = node.get("id")
        if not raw_id:
            continue
        node_id = f"{fn}:{raw_id}"
        if node_id in seen_ids:
            continue

        ls, le = _norm_lines([node.get("line_start"), node.get("line_end")])
        span_ok = ls > 0 and le >= ls
        ntype = (node.get("type") or "").upper()
        in_cohort = cohort is None or fn.strip().upper() in cohort

        if _node_writes_column(node, target_upper):
            writers_total += 1
            if not in_cohort:
                scoped_out += 1  # global writer outside the analyzed cohort.
                continue
            if not span_ok:
                writer_drops += 1  # W153: undrawable writer is not a claim.
                continue
            seen_ids.add(node_id)
            writer_nodes.append({
                "node_id": node_id, "function": fn, "type": ntype,
                "line_start": ls, "line_end": le,
                # W162 Tier 2a: capture the per-column expression HERE, while the
                # full node (with column_maps) is in scope — it is dropped from
                # the step otherwise. Display enrichment only.
                "target_expression": _expressions_for_column(node, target_upper),
            })
        else:
            if not in_cohort:
                continue  # read outside the analyzed cohort.
            if not span_ok:
                continue  # span-less read = context, not a grounded node.
            seen_ids.add(node_id)
            read_nodes.append({
                "node_id": node_id, "function": fn, "type": ntype,
                "line_start": ls, "line_end": le,
                "summary": node.get("summary") or "",
            })

    # First writer (by line) per function — the read-attachment target (7.1).
    first_writer: Dict[str, str] = {}
    for w in sorted(writer_nodes, key=lambda n: n["line_start"]):
        first_writer.setdefault(w["function"], w["node_id"])

    steps: List[Dict[str, Any]] = []
    for w in writer_nodes:
        step = {
            "node_id": w["node_id"],
            "function": w["function"],
            "kind": "derived-column",
            "line_start": w["line_start"],
            "line_end": w["line_end"],
            "label": w["function"],
            "successor": target_column,
            "edge_kind": "writes",
        }
        # W162 Tier 2a: carry the per-column expression onto the step (only when
        # present) so _assemble_fan_in can surface it as a citation enrichment.
        if w.get("target_expression"):
            step["target_expression"] = w["target_expression"]
        steps.append(step)
    for r in read_nodes:
        fw = first_writer.get(r["function"])
        if not fw or fw == r["node_id"]:
            continue  # 7.2 drop (writer-less function) / self-loop guard.
        steps.append({
            "node_id": r["node_id"],
            "function": r["function"],
            "kind": _GRAPH_READ_KIND.get(r["type"], "source-table"),
            "line_start": r["line_start"],
            "line_end": r["line_end"],
            "label": (r["summary"] or r["function"])[:60],
            "successor": fw,
            "edge_kind": "reads",
        })

    return {"steps": steps, "writer_drops": writer_drops,
            "writers_total": writers_total, "scoped_out": scoped_out}


def attested_writers_for_target(
    target: str,
    vt_graph: Optional[List[Dict[str, Any]]],
    vt_tagged: Optional[List[Dict[str, Any]]],
    multi_source: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Tuple[int, int]]]:
    """W169: per-function attested writer spans for *target*, read from the
    SAME structured fan-in sources the diagram is built on, in the SAME
    precedence the diagram uses — ``vt_graph`` (Phase-3.6 graph nodes, the
    primary path with the W153 ``_node_writes_column`` attestation) first,
    falling back to ``vt_tagged`` (Phase-3.5 tagged lines) only when the
    graph path produced no steps.

    Returns ``{FUNCTION_UPPER: [(line_start, line_end), ...]}`` for every
    writer step (``edge_kind == "writes"``). Returns an empty dict when no
    structured source is populated (the non-VARIABLE_TRACE case) or no
    attested writer survives — the caller's W169 gate then no-ops.

    Thin accessor: composes the existing :func:`fan_in_steps_from_graph` /
    :func:`fan_in_steps_from_tagged_lines` projections so the writer-op
    definition and cohort/span discipline stay single-sourced here. Pure;
    no Redis, no LLM, no side effects.
    """
    steps: List[Dict[str, Any]] = []
    if vt_graph:
        steps = fan_in_steps_from_graph(
            vt_graph, target, multi_source=multi_source
        ).get("steps", [])
    if not steps and vt_tagged:
        steps = fan_in_steps_from_tagged_lines(vt_tagged, target)

    writers: Dict[str, List[Tuple[int, int]]] = {}
    for step in steps:
        if step.get("edge_kind") != "writes":
            continue
        fn = str(step.get("function", "")).strip().upper()
        if not fn:
            continue
        ls = step.get("line_start")
        le = step.get("line_end")
        writers.setdefault(fn, []).append((ls, le))
    return writers
