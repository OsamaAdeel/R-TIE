"""W58.d: function-precheck exclusion for manifest process/sub_process names.

Process and sub_process names from each batch's manifest.yaml match the
uppercase+underscore heuristic (e.g. ``OPS_RISK_PROCESSING``,
``ABL_CAPITAL_STRUCTURE_DATA_POPULATION``) but never name a callable
PL/SQL function. Without this filter the function-precheck DECLINEs
legitimate queries that mention a workflow label to scope the question
(D2 / D3 in benchmark Run 3).
"""

import pytest

from src.agents import orchestrator
from src.agents.orchestrator import (
    extract_function_candidates,
    set_process_subprocess_names,
)


# Sample drawn from the two manifests currently in db/modules/. Includes
# both top-level processes (OPS_RISK_PROCESSING, ABL_CAPITAL_STRUCTURE_*)
# and nested sub_processes (CR_INPUT_DATA_POPULATION, OPS_RISK_LOAD).
_SAMPLE_PROCESS_NAMES = {
    "OPS_RISK_PROCESSING",
    "ABL_CAPITAL_STRUCTURE_DATA_POPULATION",
    "ABL_CAPITAL_STRUCTURE_DATA_PROCESSING",
    "STAGING_DATA_PREPARATION",
    "CAPITAL_ADJUSTMENTS",
    "CR_INPUT_DATA_POPULATION",
    "OPS_RISK_LOAD",
    "GL_AND_PRODUCT_PROCESSOR_LOADING",
    "MAPPING_AND_ENRICHMENT",
}


@pytest.fixture
def w58d_process_names():
    """Inject a known process/sub_process set into the orchestrator and
    restore the prior state on teardown so other test modules aren't
    affected by the leftover set."""
    prior = orchestrator._PROCESS_SUBPROCESS_NAMES
    set_process_subprocess_names(_SAMPLE_PROCESS_NAMES)
    yield
    orchestrator._PROCESS_SUBPROCESS_NAMES = prior


def test_w58d_excludes_process_name(w58d_process_names):
    """OPS_RISK_PROCESSING is a manifest process; should not be a function
    candidate."""
    candidates = extract_function_candidates(
        "What is the full pipeline that populates "
        "N_ANNUAL_GROSS_INCOME in OPS_RISK_PROCESSING?"
    )
    assert "OPS_RISK_PROCESSING" not in candidates


def test_w58d_excludes_subprocess_name(w58d_process_names):
    """ABL_CAPITAL_STRUCTURE_DATA_POPULATION is a manifest sub_process;
    should not be a function candidate."""
    candidates = extract_function_candidates(
        "Trace N_X across ABL_CAPITAL_STRUCTURE_DATA_POPULATION"
    )
    assert "ABL_CAPITAL_STRUCTURE_DATA_POPULATION" not in candidates


def test_w58d_case_insensitive(w58d_process_names):
    """Mixed-case process names still excluded."""
    candidates = extract_function_candidates(
        "Trace N_X across Ops_Risk_Processing flow"
    )
    assert not any(c.upper() == "OPS_RISK_PROCESSING" for c in candidates)


def test_w58d_real_function_name_still_extracted(w58d_process_names):
    """A real function from the manifest is still a candidate after this
    filter."""
    candidates = extract_function_candidates(
        "How does FN_LOAD_OPS_RISK_DATA work?"
    )
    assert "FN_LOAD_OPS_RISK_DATA" in candidates


def test_w58d_process_name_with_typo_still_candidate(w58d_process_names):
    """Inexact match is not excluded — only exact uppercase match against
    the manifest set."""
    candidates = extract_function_candidates(
        "How does OPS_RISK_PROCESS work?"  # typo: missing _ING
    )
    assert "OPS_RISK_PROCESS" in candidates


def test_w58d_d2_d3_benchmark_regression(w58d_process_names):
    """The two concrete benchmark Run 3 failures should now route — neither
    prompt's primary candidate is a process name."""
    d2_candidates = extract_function_candidates(
        "What is the full pipeline that populates "
        "N_ANNUAL_GROSS_INCOME for ABL Pakistan entities "
        "ending up in FCT_OPS_RISK_DATA?"
    )
    # FCT_OPS_RISK_DATA is a table (W58.a); the W58.d concern is just
    # that no process name slipped through as a candidate.
    assert "OPS_RISK_PROCESSING" not in d2_candidates

    d3_candidates = extract_function_candidates(
        "Trace how N_SHAREHOLDING_PERCENT is set across the "
        "OPS_RISK_PROCESSING flow."
    )
    assert "OPS_RISK_PROCESSING" not in d3_candidates


def test_w58d_empty_set_is_noop():
    """When the exclusion set is empty (pre-startup, or manifest never
    loaded), W58.d does not exclude anything — names that look like
    callable functions still come through. Restores prior state on exit
    so order-of-test independence is preserved."""
    prior = orchestrator._PROCESS_SUBPROCESS_NAMES
    set_process_subprocess_names(set())
    try:
        candidates = extract_function_candidates(
            "Trace N_X across OPS_RISK_PROCESSING"
        )
        assert "OPS_RISK_PROCESSING" in candidates
    finally:
        orchestrator._PROCESS_SUBPROCESS_NAMES = prior
