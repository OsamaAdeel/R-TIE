"""
Identify which graph node introduced a value discrepancy.
"""

from __future__ import annotations

from typing import Any


class DifferenceDetector:
    """Walk a proof chain to find the step where expected vs actual diverged."""

    def detect_delta_source(
        self,
        proof_chain: dict,
        expected_value: float,
        actual_value: float,
        tolerance: float = 0.01,
    ) -> dict:
        """Locate the step responsible for the delta between *expected* and *actual*.

        The walk advances while expected and actual stay within
        *tolerance* of each other. As soon as they diverge, that step
        is the root cause.
        """
        delta = expected_value - actual_value
        delta_percent = _pct(expected_value, actual_value)

        if abs(delta) <= tolerance:
            return {
                "delta": 0.0,
                "delta_percent": 0.0,
                "root_cause_step": None,
                "root_cause_node": None,
                "cause_type": "NONE",
                "explanation": "Expected and actual values match within tolerance.",
                "evidence": {},
                "suggested_verification_sql": "",
            }

        steps = proof_chain.get("steps") or []
        running_expected = expected_value

        for step in steps:
            actual_at_step = step.get("output_value")
            if actual_at_step is None:
                if step.get("row_count", 0) == 0 and step.get("data_available") is False:
                    return self._build_result(
                        delta=delta,
                        delta_percent=delta_percent,
                        step=step,
                        cause_type="MISSING_DATA",
                        explanation=(
                            f"Step {step['step_number']} returned no rows for the requested filters. "
                            "This is likely the root cause of the discrepancy."
                        ),
                        expected_at_step=running_expected,
                        actual_at_step=None,
                        triggering_rule="source rows = 0",
                    )
                continue

            expected_at_step = step.get("expected_output", running_expected)
            if expected_at_step is None:
                expected_at_step = running_expected

            if abs(expected_at_step - actual_at_step) <= tolerance:
                running_expected = actual_at_step
                continue

            cause_type, explanation, triggering_rule = self._classify_cause(step)
            return self._build_result(
                delta=delta,
                delta_percent=delta_percent,
                step=step,
                cause_type=cause_type,
                explanation=explanation,
                expected_at_step=expected_at_step,
                actual_at_step=actual_at_step,
                triggering_rule=triggering_rule,
            )

        return {
            "delta": delta,
            "delta_percent": delta_percent,
            "root_cause_step": None,
            "root_cause_node": None,
            "cause_type": "UNKNOWN",
            "explanation": (
                f"Delta of {delta:+.4f} detected but could not be localised to a single step. "
                "Values at each step were consistent; the discrepancy may originate outside the traced path."
            ),
            "evidence": {},
            "suggested_verification_sql": "",
        }

    def _classify_cause(self, step: dict) -> tuple[str, str, str]:
        overrides = step.get("overrides_triggered") or []
        if overrides:
            rule = overrides[0].get("rule", "") if isinstance(overrides[0], dict) else str(overrides[0])
            return (
                "OVERRIDE",
                f"Step {step['step_number']} triggered an override rule that modified the value.",
                rule,
            )

        node_type = (step.get("node_type") or "").upper()
        if node_type in ("UPDATE", "MERGE"):
            conds = step.get("conditions_met") or []
            cond = conds[0] if conds else ""
            return (
                "CONDITION",
                (
                    f"Step {step['step_number']} matched an UPDATE/MERGE condition that "
                    "changed the value."
                ),
                cond or step.get("formula", ""),
            )

        if step.get("formula"):
            return (
                "CALCULATION",
                (
                    f"Step {step['step_number']} applied a calculation that produced a different "
                    "value than expected."
                ),
                step.get("formula", ""),
            )

        return (
            "UNKNOWN",
            f"Step {step['step_number']} diverged but the cause could not be classified.",
            "",
        )

    def _build_result(
        self,
        delta: float,
        delta_percent: float,
        step: dict,
        cause_type: str,
        explanation: str,
        expected_at_step: float | None,
        actual_at_step: float | None,
        triggering_rule: str,
    ) -> dict:
        return {
            "delta": delta,
            "delta_percent": delta_percent,
            "root_cause_step": step.get("step_number"),
            "root_cause_node": {
                "id": step.get("node_id"),
                "type": step.get("node_type"),
                "function": step.get("function"),
                "source_ref": step.get("source_ref"),
            },
            "cause_type": cause_type,
            "explanation": explanation,
            "evidence": {
                "expected_at_step": expected_at_step,
                "actual_at_step": actual_at_step,
                "triggering_rule": triggering_rule,
                "source_ref": step.get("source_ref", ""),
            },
            "suggested_verification_sql": step.get("query", ""),
        }


def _pct(expected: float, actual: float) -> float:
    if expected == 0:
        return 0.0
    return round(((expected - actual) / expected) * 100.0, 4)
