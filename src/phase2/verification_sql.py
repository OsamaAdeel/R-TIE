"""
Generate ready-to-run SQL queries for engineers to verify each step.

The goal is to produce a script an engineer can paste into SQL*Plus /
SQL Developer and run independently, with bind parameters and expected
results documented inline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class VerificationSQLGenerator:
    """Produce engineer-runnable SQL for each proof step."""

    def generate_step_verification(
        self,
        step: dict,
        filters: dict[str, Any],
    ) -> dict:
        """Return a standalone verification entry for one proof step."""
        sql = (step.get("query") or "").strip()
        params = {k: v for k, v in filters.items() if v is not None and v != ""}
        expected_result = self._describe_expected(step)

        return {
            "step_number": step.get("step_number"),
            "description": step.get("transformation") or step.get("operation", ""),
            "sql": sql,
            "bind_params": params,
            "expected_result": expected_result,
        }

    def generate_full_verification_script(
        self,
        proof_chain: dict,
        filters: dict[str, Any],
    ) -> str:
        """Return a complete .sql script covering every proof step."""
        target = proof_chain.get("target_variable", "(unknown)")
        mis_date = filters.get("mis_date", "(not set)")
        account = filters.get("account_number", "(not set)")
        ts = datetime.now(timezone.utc).isoformat()

        lines: list[str] = []
        lines.append("-- ========================================================")
        lines.append("-- RTIE VERIFICATION SCRIPT")
        lines.append(f"-- Target:   {target}")
        lines.append(f"-- Account:  {account}")
        lines.append(f"-- MIS Date: {mis_date}")
        lines.append(f"-- Generated: {ts}")
        lines.append("-- ========================================================")
        lines.append("")
        lines.append("-- Bind parameters:")
        for k, v in filters.items():
            if v is None or v == "":
                continue
            lines.append(f"--   :{k} = {v!r}")
        lines.append("")

        steps = proof_chain.get("steps") or []
        if not steps:
            lines.append("-- (no steps resolved for this query)")
            return "\n".join(lines)

        for step in steps:
            entry = self.generate_step_verification(step, filters)
            lines.append(f"-- Step {entry['step_number']}: {entry['description']}")
            if entry.get("expected_result"):
                lines.append(f"-- Expected: {entry['expected_result']}")
            source_ref = step.get("source_ref")
            if source_ref:
                lines.append(f"-- Source:   {source_ref}")
            sql = entry["sql"].rstrip().rstrip(";")
            if sql:
                lines.append(f"{sql};")
            else:
                lines.append("-- (no query generated for this step)")
            lines.append("")

        return "\n".join(lines)

    def _describe_expected(self, step: dict) -> str:
        out = step.get("output_value")
        if out is not None:
            return f"~{out}"
        if step.get("row_count") == 0:
            return "0 rows (no data at this step)"
        formula = step.get("formula")
        if formula:
            return f"value produced by: {formula[:160]}"
        return "(no expected value known)"
