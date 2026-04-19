"""Unit tests for src.phase2.evidence_builder.

Focus: the missing_row evidence must surface catalog-derived overrides
(EOP overrides, block-list matches) so the Phase2Explainer can tell the
engineer what WOULD happen if a row were loaded for that GL code. When
no catalog match exists, ``known_overrides`` stays empty and the
explainer falls back to the plain "row does not exist" message.
"""

from __future__ import annotations

import pytest

from src.phase2.evidence_builder import EvidenceBuilder
from src.phase2.explainer import Phase2Explainer


# ---------------------------------------------------------------------
# EvidenceBuilder.build_for_missing_row
# ---------------------------------------------------------------------

class TestBuildForMissingRow:
    def test_empty_filters_yields_empty_known_overrides(self):
        """No GL code, no catalog match -- known_overrides is empty."""
        builder = EvidenceBuilder()

        evidence = builder.build_for_missing_row({})

        assert evidence["kind"] == "missing_row"
        assert evidence["known_overrides"] == []
        assert evidence["row_facts"] == {}

    def test_gl_code_without_catalog_match(self):
        """GL code present but catalog reports no match -- still empty."""
        builder = EvidenceBuilder()

        evidence = builder.build_for_missing_row(
            filters={"gl_code": "999999999-0000", "mis_date": "2025-12-31"},
            eop_override=None,
            gl_blocked=False,
        )

        assert evidence["known_overrides"] == []
        # filters survive the build (minus Nones/empties).
        assert evidence["filters"] == {
            "gl_code": "999999999-0000",
            "mis_date": "2025-12-31",
        }

    def test_eop_override_surfaced_with_node_and_line(self):
        """EOP override catalog hit -- node_id + line_number carried into evidence."""
        builder = EvidenceBuilder()

        evidence = builder.build_for_missing_row(
            filters={"gl_code": "108012501-1107"},
            eop_override={
                "function": "POPULATE_PP_FROMGL",
                "node_id": "POPULATE_PP_FROMGL:node_5",
                "line": 388,
                "reason": "Single-column N_EOP_BAL zero-override via V_GL_CODE",
            },
            gl_blocked=False,
        )

        assert len(evidence["known_overrides"]) == 1
        override = evidence["known_overrides"][0]
        assert override["type"] == "eop_override"
        assert override["gl_code"] == "108012501-1107"
        assert override["node"] == "POPULATE_PP_FROMGL:node_5"
        assert override["line"] == 388
        assert "N_EOP_BAL" in override["effect"]

    def test_block_list_surfaced_with_node(self):
        """Block-list catalog hit -- node reference carried into evidence."""
        builder = EvidenceBuilder()

        evidence = builder.build_for_missing_row(
            filters={"gl_code": "401020114-0000"},
            eop_override=None,
            gl_blocked=True,
        )

        assert len(evidence["known_overrides"]) == 1
        override = evidence["known_overrides"][0]
        assert override["type"] == "block_list"
        assert override["gl_code"] == "401020114-0000"
        assert "POPULATE_PP_FROMGL:node_4" in override["node"]
        assert "F_EXPOSURE_ENABLED_IND" in override["effect"]

    def test_eop_and_block_both_match(self):
        """A GL code on both lists produces two override entries."""
        builder = EvidenceBuilder()

        evidence = builder.build_for_missing_row(
            filters={"gl_code": "500000000-0000"},
            eop_override={
                "node_id": "POPULATE_PP_FROMGL:node_5",
                "line": 400,
            },
            gl_blocked=True,
        )

        types = [ov["type"] for ov in evidence["known_overrides"]]
        assert types == ["eop_override", "block_list"]

    def test_v_gl_code_filter_key_also_works(self):
        """Uppercase filter key ``V_GL_CODE`` is read as the GL code."""
        builder = EvidenceBuilder()

        evidence = builder.build_for_missing_row(
            filters={"V_GL_CODE": "108012501-1107"},
            eop_override={"node_id": "POPULATE_PP_FROMGL:node_5", "line": 388},
        )

        assert evidence["known_overrides"][0]["gl_code"] == "108012501-1107"


# ---------------------------------------------------------------------
# Phase2Explainer -- deterministic fallback for missing_row
# ---------------------------------------------------------------------
#
# We can't exercise the real LLM path in a unit test, so we drive the
# deterministic fallback directly. That path uses the exact same evidence
# dict and prompt contract as the LLM path, so a correctly-shaped fallback
# proves the evidence + prompt seam is wired correctly.
# ---------------------------------------------------------------------

class TestMissingRowFallback:
    def test_no_overrides_yields_plain_message(self):
        """Empty known_overrides -> baseline one-sentence message only."""
        explainer = Phase2Explainer()
        evidence = {
            "kind": "missing_row",
            "filters": {"gl_code": "DOES-NOT-EXIST"},
            "known_overrides": [],
        }

        text = explainer._fallback(
            "missing_row", evidence, {"gl_code": "DOES-NOT-EXIST"}
        )

        assert "No row was found" in text
        assert "Note:" not in text

    def test_eop_override_produces_note_with_exact_references(self):
        """EOP override -> Note paragraph with the exact node/line/effect."""
        explainer = Phase2Explainer()
        evidence = {
            "kind": "missing_row",
            "filters": {"gl_code": "108012501-1107"},
            "known_overrides": [
                {
                    "type": "eop_override",
                    "gl_code": "108012501-1107",
                    "node": "POPULATE_PP_FROMGL:node_5",
                    "line": 388,
                    "effect": "N_EOP_BAL would be forced to 0",
                },
            ],
        }

        text = explainer._fallback(
            "missing_row", evidence, {"gl_code": "108012501-1107"}
        )

        assert "Note:" in text
        assert "108012501-1107" in text
        assert "POPULATE_PP_FROMGL:node_5" in text
        assert "388" in text
        assert "N_EOP_BAL would be forced to 0" in text
        # Conditional phrasing only -- no claim that the override has fired.
        assert "if a row were loaded" in text.lower()

    def test_block_list_note_mentions_exposure_column(self):
        explainer = Phase2Explainer()
        evidence = {
            "kind": "missing_row",
            "filters": {"gl_code": "401020114-0000"},
            "known_overrides": [
                {
                    "type": "block_list",
                    "gl_code": "401020114-0000",
                    "node": "POPULATE_PP_FROMGL:node_4",
                    "effect": "F_EXPOSURE_ENABLED_IND would be set to 'N'",
                },
            ],
        }

        text = explainer._fallback(
            "missing_row", evidence, {"gl_code": "401020114-0000"}
        )

        assert "F_EXPOSURE_ENABLED_IND" in text
        assert "POPULATE_PP_FROMGL:node_4" in text

    def test_forbidden_speculation_tokens_absent(self):
        """The enhanced fallback must not speculate about WHY the row is missing."""
        explainer = Phase2Explainer()
        evidence = {
            "kind": "missing_row",
            "filters": {"gl_code": "108012501-1107"},
            "known_overrides": [
                {
                    "type": "eop_override",
                    "gl_code": "108012501-1107",
                    "node": "POPULATE_PP_FROMGL:node_5",
                    "line": 388,
                    "effect": "N_EOP_BAL would be forced to 0",
                },
            ],
        }

        text = explainer._fallback(
            "missing_row", evidence, {"gl_code": "108012501-1107"}
        ).lower()

        for banned in (
            "possible reasons",
            "might be because",
            "may have",
            "likely",
            "probably",
        ):
            assert banned not in text, f"fallback contained banned token: {banned!r}"


# ---------------------------------------------------------------------
# Phase2Explainer.sanity_check -- numeric-invention must tolerate
# legitimate references carried over from evidence.
# ---------------------------------------------------------------------

class TestSanityCheckMissingRow:
    def test_override_reference_is_not_flagged(self):
        """GL codes and line numbers from known_overrides are legitimate cites."""
        explainer = Phase2Explainer()
        evidence = {
            "kind": "missing_row",
            "filters": {"gl_code": "108012501-1107", "mis_date": "2025-12-31"},
            "known_overrides": [
                {
                    "type": "eop_override",
                    "gl_code": "108012501-1107",
                    "node": "POPULATE_PP_FROMGL:node_5",
                    "line": 388,
                    "effect": "N_EOP_BAL would be forced to 0",
                },
            ],
        }
        text = (
            "The row does not exist for the given filters; please verify "
            "the account number and date.\n\n"
            "Note: GL code 108012501-1107 is on the eop_override catalog "
            "(POPULATE_PP_FROMGL:node_5, line 388). If a row were loaded, "
            "N_EOP_BAL would be forced to 0."
        )

        warnings = explainer.sanity_check(
            text=text,
            route="missing_row",
            evidence=evidence,
            row=None,
            known_functions={"POPULATE_PP_FROMGL"},
        )

        assert "invented_numeric_value" not in warnings

    def test_truly_invented_currency_is_flagged(self):
        """A value that does NOT appear anywhere in evidence is still caught."""
        explainer = Phase2Explainer()
        evidence = {
            "kind": "missing_row",
            "filters": {"gl_code": "108012501-1107"},
            "known_overrides": [],
        }
        text = (
            "The row does not exist. The balance was -12345.67 at the time "
            "of the query."
        )

        warnings = explainer.sanity_check(
            text=text,
            route="missing_row",
            evidence=evidence,
            row=None,
            known_functions=set(),
        )

        assert any("invented_numeric_value" in w for w in warnings)
