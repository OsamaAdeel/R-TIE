"""
Assemble a mathematical proof chain from graph path + fetched values.

Each step in the chain couples a graph node (the logic) with an actual
fetched value (the data). Where possible, the builder also computes
what the output *should* be given the input -- this enables downstream
difference detection.
"""

from __future__ import annotations

import re
from typing import Any


_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


class ProofBuilder:
    """Build a structured proof chain of how a value was computed."""

    def build_proof_chain(
        self,
        graph_path: list[dict],
        value_chain: list[dict],
        target_variable: str,
    ) -> dict:
        """Assemble the full proof structure.

        Parameters
        ----------
        graph_path
            Phase 1 graph nodes in execution order.
        value_chain
            Output of ValueFetcher.fetch_value_chain() -- same length
            and order as graph_path when possible.
        target_variable
            Column name being traced.
        """
        steps: list[dict] = []
        previous_value: float | None = None
        origin_value: float | None = None
        final_value: float | None = None

        for idx, entry in enumerate(value_chain):
            node = entry.get("node") or {}
            vresult = entry.get("value_result") or {}
            fn_name = entry.get("function") or _extract_function_name(node.get("id", ""))

            input_value = previous_value
            output_value = self._pick_value(vresult, target_variable, node)

            if origin_value is None and output_value is not None:
                origin_value = output_value

            computed = self.compute_expected_output(node, input_value, _flatten_filters(vresult))
            transformation = self._describe_transformation(node, target_variable)
            formula = self._extract_formula(node, target_variable)
            conditions_met = self._conditions_from_result(node, vresult)
            overrides = self._detect_overrides(node, vresult)
            data_available = vresult.get("status") == "found" and output_value is not None

            step = {
                "step_number": idx + 1,
                "function": fn_name,
                "operation": node.get("type", "UNKNOWN"),
                "node_type": node.get("type", "UNKNOWN"),
                "node_id": node.get("id", ""),
                "input_value": input_value,
                "output_value": output_value,
                "expected_output": computed.get("expected_value"),
                "transformation": transformation,
                "formula": formula,
                "conditions_met": conditions_met,
                "overrides_triggered": overrides,
                "data_available": data_available,
                "notes": _note_for_status(vresult.get("status"), vresult.get("error")),
                "source_ref": self._source_ref(node, fn_name),
                "query": vresult.get("query", ""),
                "row_count": vresult.get("row_count", 0),
            }
            steps.append(step)

            if output_value is not None:
                previous_value = output_value
                final_value = output_value

        total_delta = None
        if origin_value is not None and final_value is not None:
            total_delta = final_value - origin_value

        confidence = _confidence(value_chain)
        summary = self._summarise(steps, target_variable, origin_value, final_value, total_delta)

        return {
            "target_variable": target_variable,
            "final_value": final_value,
            "origin_value": origin_value,
            "steps": steps,
            "total_delta": total_delta,
            "summary": summary,
            "confidence": confidence,
        }

    def compute_expected_output(
        self,
        node: dict,
        input_value: float | None,
        filters: dict[str, Any],
    ) -> dict:
        """Compute what *output_value* should be for this node given *input_value*.

        Handles DIRECT, ARITHMETIC, CONDITIONAL, FALLBACK, OVERRIDE by
        best-effort interpretation of the parsed calculation block. When
        a symbolic expression references variables not available in
        *filters* the function returns ``expected_value=None`` rather
        than guessing.
        """
        calcs = node.get("calculation") or []
        calc = calcs[0] if calcs and isinstance(calcs[0], dict) else None
        calc_type = (calc.get("type") if calc else "DIRECT").upper()

        if calc_type == "DIRECT" or calc is None:
            return {
                "expected_value": input_value,
                "calculation_applied": "direct copy",
                "override_triggered": False,
                "override_details": None,
            }

        expr = (calc.get("expression") or "").strip() if calc else ""

        if calc_type == "ARITHMETIC" and input_value is not None and expr:
            expected = _try_eval_arithmetic(expr, input_value, filters)
            return {
                "expected_value": expected,
                "calculation_applied": expr,
                "override_triggered": False,
                "override_details": None,
            }

        if calc_type in ("CONDITIONAL", "OVERRIDE"):
            overrides = node.get("overrides") or []
            triggered = self._match_override(overrides, filters)
            return {
                "expected_value": None,
                "calculation_applied": expr or "conditional",
                "override_triggered": triggered is not None,
                "override_details": triggered,
            }

        if calc_type == "FALLBACK":
            return {
                "expected_value": input_value,
                "calculation_applied": expr or "fallback",
                "override_triggered": False,
                "override_details": None,
            }

        return {
            "expected_value": None,
            "calculation_applied": expr or calc_type,
            "override_triggered": False,
            "override_details": None,
        }

    def _pick_value(
        self,
        vresult: dict,
        target_variable: str,
        node: dict,
    ) -> float | None:
        rows = vresult.get("rows") or []
        if not rows:
            return None

        target_upper = (target_variable or "").upper()
        row = rows[0]
        if target_upper in row:
            return _coerce_number(row[target_upper])

        if node.get("type", "").upper() == "SCALAR_COMPUTE":
            out_var = (node.get("output_variable") or "").upper()
            if out_var and out_var in row:
                return _coerce_number(row[out_var])

        if "AGG_VALUE" in row:
            return _coerce_number(row["AGG_VALUE"])

        for val in row.values():
            coerced = _coerce_number(val)
            if coerced is not None:
                return coerced
        return None

    def _describe_transformation(self, node: dict, target_variable: str) -> str:
        node_type = (node.get("type") or "").upper()
        tgt = node.get("target_table")
        srcs = ", ".join(node.get("source_tables") or []) or "(none)"

        if node_type == "INSERT":
            return f"Insert {target_variable} into {tgt} from {srcs}."
        if node_type == "UPDATE":
            assignments = (node.get("column_maps") or {}).get("assignments") or []
            for col, expr in assignments:
                if (col or "").upper() == target_variable.upper():
                    return f"Update {target_variable} in {tgt} using: {expr.strip()[:160]}"
            return f"Update rows in {tgt}."
        if node_type == "SCALAR_COMPUTE":
            out = node.get("output_variable") or ""
            calcs = node.get("calculation") or []
            expr = calcs[0].get("expression", "") if calcs and isinstance(calcs[0], dict) else ""
            return f"Compute intermediate variable {out} = {expr[:120]}"
        if node_type == "MERGE":
            return f"Merge into {tgt}."
        return node.get("summary", f"{node_type} operation on {tgt or srcs}")

    def _extract_formula(self, node: dict, target_variable: str) -> str:
        assignments = (node.get("column_maps") or {}).get("assignments") or []
        for col, expr in assignments:
            if (col or "").upper() == target_variable.upper():
                return (expr or "").strip()
        calcs = node.get("calculation") or []
        if calcs and isinstance(calcs[0], dict):
            return (calcs[0].get("expression") or "").strip()
        return ""

    def _conditions_from_result(self, node: dict, vresult: dict) -> list[str]:
        conds = node.get("conditions") or []
        return [c if isinstance(c, str) else c.get("expression", str(c)) for c in conds]

    def _detect_overrides(self, node: dict, vresult: dict) -> list[dict]:
        overrides = node.get("overrides") or []
        if not overrides:
            return []
        result: list[dict] = []
        for ov in overrides:
            if isinstance(ov, dict):
                result.append({
                    "rule": ov.get("rule") or ov.get("expression", ""),
                    "value": ov.get("value"),
                    "line": ov.get("line"),
                })
        return result

    def _match_override(
        self,
        overrides: list[dict],
        filters: dict[str, Any],
    ) -> dict | None:
        for ov in overrides:
            if not isinstance(ov, dict):
                continue
            rule = (ov.get("rule") or ov.get("expression") or "").upper()
            if not rule:
                continue
            matched = True
            for key, val in filters.items():
                if val is None:
                    continue
                if key.upper() in rule and str(val).upper() in rule:
                    continue
            if matched:
                return ov
        return None

    def _source_ref(self, node: dict, fn_name: str) -> str:
        ls = node.get("line_start")
        le = node.get("line_end")
        if ls and le:
            return f"{fn_name}.sql, lines {ls}-{le}"
        return f"{fn_name}.sql"

    def _summarise(
        self,
        steps: list[dict],
        target_variable: str,
        origin: float | None,
        final: float | None,
        delta: float | None,
    ) -> str:
        n = len(steps)
        if n == 0:
            return f"No steps resolved for {target_variable}."
        if origin is None and final is None:
            return f"{target_variable}: {n} step(s) in chain; no data values could be fetched."
        if delta is None or abs(delta) < 1e-9:
            return (
                f"{target_variable} traced through {n} step(s). "
                f"Value {origin} passed through unchanged to final value {final}."
            )
        return (
            f"{target_variable} traced through {n} step(s). "
            f"Origin {origin} -> final {final} (delta {delta:+.4f})."
        )


def _coerce_number(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if _NUMBER_RE.match(s):
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _try_eval_arithmetic(
    expr: str,
    input_value: float,
    filters: dict[str, Any],
) -> float | None:
    """Attempt to evaluate a simple arithmetic expression.

    Only supports +, -, *, /, numeric literals, and the placeholder
    keyword ``input`` (mapped to input_value). Anything else results
    in None -- we never eval arbitrary expressions.
    """
    e = (expr or "").lower().replace("input_value", "input")
    e = re.sub(r"[a-z_][a-z0-9_]*", lambda m: "input" if m.group(0) == "input" else "0", e)
    if not re.fullmatch(r"[\s0-9\+\-\*\/\.\(\)input]+", e):
        return None
    try:
        return float(eval(e, {"__builtins__": {}}, {"input": input_value}))  # noqa: S307
    except Exception:
        return None


def _note_for_status(status: str | None, error: str | None) -> str:
    if status == "found":
        return ""
    if status == "empty":
        return "No rows matched the filters at this node."
    if status == "error":
        return f"Fetch error: {error or 'unknown'}"
    return ""


def _confidence(value_chain: list[dict]) -> float:
    if not value_chain:
        return 0.0
    found = sum(1 for e in value_chain if (e.get("value_result") or {}).get("status") == "found")
    return round(found / len(value_chain), 4)


def _flatten_filters(vresult: dict) -> dict:
    return dict(vresult.get("bind_params") or {})


def _extract_function_name(node_id: str) -> str:
    if ":" in node_id:
        node_id = node_id.split(":", 1)[1]
    m = re.match(r"^(.+?)(?:_N\d+|_COMMENTED_\d+|_OP\d+|_INNER)$", node_id)
    if m:
        return m.group(1)
    return node_id or "UNKNOWN"
