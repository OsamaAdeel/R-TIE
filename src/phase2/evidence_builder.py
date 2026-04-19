"""
Build evidence for the LLM from verified facts only.

Evidence is strictly what we have seen in Oracle or read from the
graph catalog. No hypotheticals, no imagined function names, no
assumptions about how the row might have been computed.
"""

from __future__ import annotations

from typing import Any


# Columns that a row usually has but we don't want to dump verbatim to
# the LLM -- mostly timestamps and housekeeping fields.
_SKIP_COLUMNS = {
    "FIC_MIS_DATE",  # re-emitted separately
    "CREATED_DATE",
    "LAST_UPDATED_DATE",
    "N_BATCH_RUN_ID",
    "DT_BATCH_RUN_DATE",
}


class EvidenceBuilder:
    """Produce evidence dicts that feed directly into LLM prompts."""

    # ---------------------------------------------------------------
    # Variants of evidence
    # ---------------------------------------------------------------

    def build_for_plsql_trace(
        self,
        row: dict[str, Any],
        classification: dict,
        graph_path: list[dict],
        value_chain: list[dict],
    ) -> dict:
        """Row produced by one of our PL/SQL functions."""
        steps: list[dict] = []
        for entry in value_chain or []:
            node = entry.get("node") or {}
            vr = entry.get("value_result") or {}
            steps.append({
                "node_id": node.get("id"),
                "function": entry.get("function") or node.get("function"),
                "type": node.get("type"),
                "lines": self._line_range(node),
                "status": vr.get("status"),
                "row_count": vr.get("row_count"),
                "query": vr.get("query"),
            })

        return {
            "kind": "plsql_trace",
            "row_facts": self._row_facts(row),
            "origin": classification.get("origin_value"),
            "origin_details": classification.get("origin_details", {}),
            "graph_steps": steps,
            "verification_sql": self._verification_sql(row, classification, steps),
        }

    def build_for_etl_origin(
        self,
        row: dict[str, Any],
        classification: dict,
    ) -> dict:
        """Row loaded by an external ETL -- NOT traceable through the graph.

        The evidence explicitly calls out that no PL/SQL function in the
        indexed codebase computed the traced value. It also lists any
        columns on the row that WERE modified by graph nodes (e.g.
        ``F_EXPOSURE_ENABLED_IND`` set by ``POPULATE_PP_FROMGL:node_4``),
        so the explainer can state exactly what was and wasn't touched.
        """
        details = classification.get("origin_details", {}) or {}
        origin_value = classification.get("origin_value")

        plsql_modifications: list[dict] = []
        if classification.get("flags", {}).get("in_block_list"):
            plsql_modifications.append({
                "column": "F_EXPOSURE_ENABLED_IND",
                "set_to": "N",
                "by": "POPULATE_PP_FROMGL:node_4",
                "reason": "GL code is on the hardcoded block list",
            })
        eop_override = classification.get("eop_override")
        if eop_override:
            plsql_modifications.append({
                "column": "N_EOP_BAL",
                "set_to": 0,
                "by": eop_override.get("node"),
                "line": eop_override.get("line"),
                "reason": eop_override.get("reason"),
            })

        return {
            "kind": "etl_origin",
            "row_facts": self._row_facts(row),
            "origin": origin_value,
            "origin_source": details.get("source", "external ETL"),
            "origin_description": details.get(
                "description", "row loaded by external ETL"
            ),
            "plsql_modifications": plsql_modifications,
            "plsql_non_modifications": self._non_modifications(row, plsql_modifications),
            "fix_path": details.get(
                "fix_path",
                "Investigate the upstream ETL pipeline logs for the MIS date",
            ),
            "verification_sql": self._verification_sql(row, classification, []),
        }

    def build_for_unknown_origin(
        self,
        row: dict[str, Any],
        classification: dict,
    ) -> dict:
        """Row whose V_DATA_ORIGIN is not in either catalog."""
        return {
            "kind": "unknown_origin",
            "row_facts": self._row_facts(row),
            "origin": classification.get("origin_value"),
            "origin_details": classification.get("origin_details", {}),
            "fix_path": (
                "Extend the RTIE origin catalog with this V_DATA_ORIGIN value "
                "or inspect the row's upstream ETL."
            ),
            "verification_sql": self._verification_sql(row, classification, []),
        }

    def build_for_missing_row(
        self,
        filters: dict[str, Any],
        eop_override: dict | None = None,
        gl_blocked: bool = False,
    ) -> dict:
        """No row matched the user's filters.

        If the filters named a GL code that the origins catalog already knows
        about (either on the hardcoded EOP override list or on the block
        list), the matching catalog entries are surfaced in
        ``known_overrides`` so the LLM can explain what WOULD happen if a
        row were loaded for that GL code. When no catalog match is found,
        ``known_overrides`` is an empty list.
        """
        gl_code = filters.get("gl_code") or filters.get("V_GL_CODE")

        known_overrides: list[dict] = []
        if eop_override:
            known_overrides.append({
                "type": "eop_override",
                "gl_code": gl_code,
                "node": eop_override.get("node_id"),
                "line": eop_override.get("line"),
                "effect": "N_EOP_BAL would be forced to 0",
            })
        if gl_blocked:
            known_overrides.append({
                "type": "block_list",
                "gl_code": gl_code,
                "node": "POPULATE_PP_FROMGL:node_4",
                "effect": "F_EXPOSURE_ENABLED_IND would be set to 'N'",
            })

        return {
            "kind": "missing_row",
            "filters": {k: v for k, v in filters.items() if v is not None and v != ""},
            "row_facts": {},
            "known_overrides": known_overrides,
            "verification_sql": "",
        }

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _row_facts(self, row: dict[str, Any]) -> dict:
        """Non-null column values from the row, minus housekeeping columns."""
        facts: dict = {}
        for k, v in (row or {}).items():
            if k in _SKIP_COLUMNS:
                continue
            if v is None or v == "":
                continue
            facts[k] = self._serialisable(v)
        return facts

    def _non_modifications(
        self,
        row: dict[str, Any],
        plsql_modifications: list[dict],
    ) -> list[str]:
        """Return columns on the row that the graph did NOT modify.

        We only enumerate the handful of columns that PL/SQL could have
        touched on this row but didn't, so the LLM can state clearly
        "PL/SQL did not compute this value".
        """
        touched = {m.get("column") for m in plsql_modifications}
        candidates = {"N_EOP_BAL", "N_ANNUAL_GROSS_INCOME", "N_AMOUNT_LCY"}
        return sorted(c for c in candidates if c in row and c not in touched)

    def _line_range(self, node: dict) -> str:
        ls = node.get("line_start")
        le = node.get("line_end")
        if ls and le:
            return f"{ls}-{le}"
        return "?"

    def _verification_sql(
        self,
        row: dict,
        classification: dict,
        steps: list[dict],
    ) -> str:
        """Produce a SELECT the engineer can paste into SQL*Plus.

        This mirrors the filters the pipeline used but keeps things
        parameterless (literal values) so the engineer can run it as-is
        -- with the understanding that the row has already been fetched
        and shown in ``row_facts``.
        """
        fic = row.get("FIC_MIS_DATE")
        acct = row.get("V_ACCOUNT_NUMBER")
        gl = row.get("V_GL_CODE")
        lv = row.get("V_LV_CODE")
        table = (classification.get("origin_details") or {}).get("target_table") or "STG_PRODUCT_PROCESSOR"

        where: list[str] = []
        if fic:
            where.append(f"FIC_MIS_DATE = DATE '{self._as_date(fic)}'")
        if acct:
            where.append(f"V_ACCOUNT_NUMBER = '{self._escape(acct)}'")
        if gl:
            where.append(f"V_GL_CODE = '{self._escape(gl)}'")
        if lv:
            where.append(f"V_LV_CODE = '{self._escape(lv)}'")
        if not where:
            return ""

        return (
            f"SELECT V_DATA_ORIGIN, V_GL_CODE, V_LV_CODE, N_EOP_BAL, "
            f"F_EXPOSURE_ENABLED_IND\n"
            f"FROM {table}\n"
            f"WHERE " + " AND ".join(where) + ";"
        )

    def _as_date(self, value: Any) -> str:
        s = str(value)
        # Oracle DATE values come back as "YYYY-MM-DD HH:MM:SS"; strip time.
        return s.split(" ")[0][:10]

    def _escape(self, value: Any) -> str:
        return str(value).replace("'", "''")

    def _serialisable(self, value: Any) -> Any:
        """Coerce non-JSON-serialisable values into strings."""
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        return str(value)
