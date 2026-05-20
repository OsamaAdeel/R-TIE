"""Pinned-count contract tests for the ABL_CAR_CSTM_V4 manifest.

These guard against silent loader regressions like the one where
``_walk_tasks`` only iterated ``process.sub_processes`` and dropped
process-level flat tasks on the floor (149/141 visible instead of
187/150). If the real manifest legitimately changes, update the
constants here in the same commit so the new contract is explicit.
"""

from pathlib import Path

import pytest

from src.parsing.manifest import load_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = PROJECT_ROOT / "db" / "modules" / "ABL_CAR_CSTM_V4"

# W106 (2026-05-20) — rebaseline after b68918a corpus expansion (Stage 1-3,
# 372 → 402 .sql files, +30 net-new active functions). The prior constants
# (203/166/37) tracked the pre-expansion authoring; the current
# ABL_CAR_CSTM_V4/manifest.yaml ships 460 total / 323 active / 137 inactive
# task entries. The growth landed across multiple processes — the runchart
# coverage was widened rather than scoped to a single sub-process — so no
# attempt is made here to enumerate the delta per container. When the
# manifest legitimately changes again, update these in the same commit so
# the new contract is explicit (this is the regression guard the file
# header describes).
EXPECTED_TOTAL = 460
EXPECTED_ACTIVE = 323
EXPECTED_INACTIVE = 137


@pytest.fixture(scope="module")
def manifest():
    return load_manifest(str(MODULE_DIR))


def test_iter_all_tasks_count(manifest):
    assert len(list(manifest.iter_all_tasks())) == EXPECTED_TOTAL


def test_iter_active_tasks_count(manifest):
    assert len(list(manifest.iter_active_tasks())) == EXPECTED_ACTIVE


def test_iter_inactive_tasks_count(manifest):
    assert len(list(manifest.iter_inactive_tasks())) == EXPECTED_INACTIVE


def test_flat_process_tasks_visible(manifest):
    """Pin: tasks declared directly under a process (no sub_process layer)
    must be visible to the iterator. Regression guard for the bug where
    `process.tasks` was parsed but never walked.
    """
    names = {t.name for t in manifest.iter_all_tasks()}
    # Sample of 4 tasks declared flat under ABL_CAPITAL_STRUCTURE_DATA_POPULATION.
    expected_flat = {
        "T2T_FCT_CCP_DETAILS_STD_ACCT_HEAD_POP",
        "PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP",
        "T2T_FSI_GL_DATA_STD_ACCT_HEAD_RWA_DATA_POP",
        "T2T_ACCT_LEVEL_GEN_PROV_STD_ACCT_HEAD_POP",
    }
    missing = expected_flat - names
    assert not missing, f"flat process tasks invisible to iter_all_tasks: {missing}"
