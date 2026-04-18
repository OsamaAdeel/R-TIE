"""
Classify the origin of a fetched row.

This is the key decision point: does the row come from PL/SQL (and
therefore traceable through the graph) or from an external ETL (and
must be explained by pointing the engineer to the upstream system)?
"""

from __future__ import annotations

from typing import Any

from src.phase2.origins_catalog import (
    classify_origin,
    get_eop_override,
    is_gl_blocked,
)


class OriginClassifier:
    """Classify a row based on V_DATA_ORIGIN + flag columns."""

    def classify_row(self, row: dict[str, Any]) -> dict:
        """Produce a full origin classification for *row*.

        Returns
        -------
        dict with keys:
            ``origin_category``     "PLSQL" | "ETL" | "UNKNOWN"
            ``origin_value``        raw V_DATA_ORIGIN string
            ``origin_details``      matching catalog entry
            ``traceable_via_graph`` True only for PLSQL with node_id
            ``gl_blocked``          True if GL on block list
            ``eop_override``        override record or None
            ``flags``               {"exposure_enabled", "eop_zero_forced", "in_block_list"}
            ``row_signature``       subset of key columns for display
        """
        v_data_origin = row.get("V_DATA_ORIGIN")
        gl_code = row.get("V_GL_CODE")
        lv_code = row.get("V_LV_CODE")
        branch_code = row.get("V_BRANCH_CODE")
        f_exposure = row.get("F_EXPOSURE_ENABLED_IND")

        classification = classify_origin(v_data_origin)
        blocked = is_gl_blocked(gl_code)
        override = get_eop_override(gl_code)

        flags = {
            "exposure_enabled": self._flag_yes(f_exposure),
            "eop_zero_forced": override is not None,
            "in_block_list": blocked,
        }

        return {
            "origin_category": classification["category"],
            "origin_value": v_data_origin,
            "origin_details": classification["details"],
            "traceable_via_graph": classification["traceable"],
            "gl_blocked": blocked,
            "eop_override": override,
            "flags": flags,
            "row_signature": {
                "v_data_origin": v_data_origin,
                "v_gl_code": gl_code,
                "v_lv_code": lv_code,
                "v_branch_code": branch_code,
                "f_exposure_enabled_ind": f_exposure,
            },
        }

    def _flag_yes(self, value: Any) -> bool:
        if value is None:
            return False
        return str(value).strip().upper() in ("Y", "YES", "1", "TRUE")
