"""W172 — offline unit tests for the function-existence RESCUE in
:func:`src.main.resolve_graph_binding` (the ``(else)`` case).

W164 fixed the binding when the classifier ECHOED a real function name into
``target_variable``. W172 fixes the disjoint case where the classifier withholds
the name entirely: ``target_var`` AND ``obj_name`` are both empty, so the binding
used to pass the WHOLE raw_query to the variable index — a guaranteed miss — even
when raw_query plainly NAMES a real graph function ("How does
FN_LOAD_OPS_RISK_DATA work?"). The rescue reuses the SAME exact machinery (extract
candidates -> EXACT ``function_exists_in_graph`` probe -> normalize ->
``schema_for_function`` repoint) and falls through to the unchanged
whole-query-variable default on no candidate or a non-exact candidate.

FP safety is the crux: the rescue keys on the EXACT key probe, never the fuzzy
``find_similar_function_names`` — a near-miss must fall through, never force-bind a
wrong function (W166 wrong-anchor containment). One test pins that the fuzzy helper
is never called.

Harness mirrors ``test_w164_graph_binding.py``: the identity primitives are
monkeypatched at the ``main`` module namespace and their calls recorded, so
branch firing is asserted via call-tracking, not just the output tuple.
"""

from __future__ import annotations

from typing import List

import pytest

from src import main as main_mod


class _SentinelRedis:
    """Non-None identity stand-in; the primitives are monkeypatched so no
    method is ever called on it — it only has to be truthy."""


@pytest.fixture
def probes(monkeypatch):
    """Monkeypatch the identity primitives and record their calls.

    Also installs a ``find_similar_function_names`` that FAILS the test if ever
    called — the rescue must be EXACT-only (W166 containment)."""

    class _Cfg:
        def __init__(self):
            self.function_exists = False
            self.function_schema = None  # schema_for_function return
            self.candidates: List[str] = []  # extract_function_candidates return
            self.calls = {
                "function_exists_in_graph": [],
                "schema_for_function": [],
                "extract_function_candidates": [],
                "find_similar_function_names": [],
            }

    cfg = _Cfg()

    def _fake_function_exists(name, redis_client, schemas=None):
        cfg.calls["function_exists_in_graph"].append(name)
        return cfg.function_exists

    def _fake_schema_for_function(name, redis_client, schemas=None):
        cfg.calls["schema_for_function"].append(name)
        return cfg.function_schema

    def _fake_extract(raw_query):
        cfg.calls["extract_function_candidates"].append(raw_query)
        return list(cfg.candidates)

    def _forbidden_fuzzy(*args, **kwargs):
        cfg.calls["find_similar_function_names"].append(args)
        raise AssertionError(
            "W172 rescue must never call find_similar_function_names "
            "(EXACT-only — W166 wrong-anchor containment)"
        )

    monkeypatch.setattr(main_mod, "function_exists_in_graph", _fake_function_exists)
    monkeypatch.setattr(main_mod, "schema_for_function", _fake_schema_for_function)
    monkeypatch.setattr(main_mod, "extract_function_candidates", _fake_extract)
    monkeypatch.setattr(main_mod, "find_similar_function_names", _forbidden_fuzzy)
    return cfg


def _bind(
    *,
    target_var="",
    obj_name="",
    raw_query="raw query text",
    g_schema="OFSMDM",
    column_owners=None,
    redis_client=None,
):
    return main_mod.resolve_graph_binding(
        target_var=target_var,
        obj_name=obj_name,
        raw_query=raw_query,
        g_schema=g_schema,
        column_owners=[] if column_owners is None else column_owners,
        redis_client=redis_client if redis_client is not None else _SentinelRedis(),
    )


# ---------------------------------------------------------------------------
# THE FIX — raw_query names an EXACT graph function -> function (was variable)
# ---------------------------------------------------------------------------


def test_rescue_binds_function_when_raw_query_names_exact_graph_function(probes):
    # The live FN_LOAD_OPS_RISK_DATA case: classifier withheld the name
    # (target_var None), no W76/bi_routing (obj_name empty). The real
    # extract_function_candidates output captured in the diagnostic is
    # ['FN_LOAD_OPS_RISK_DATA'].
    probes.candidates = ["FN_LOAD_OPS_RISK_DATA"]
    probes.function_exists = True
    probes.function_schema = "OFSMDM"
    qtype, term, schema = _bind(
        target_var="",
        obj_name="",
        raw_query="How does FN_LOAD_OPS_RISK_DATA work?",
        g_schema="OFSMDM",
    )
    assert (qtype, term, schema) == ("function", "FN_LOAD_OPS_RISK_DATA", "OFSMDM")
    # Proof the rescue (not some other branch) fired via the EXACT probe.
    assert probes.calls["function_exists_in_graph"] == ["FN_LOAD_OPS_RISK_DATA"]
    assert probes.calls["extract_function_candidates"] == [
        "How does FN_LOAD_OPS_RISK_DATA work?"
    ]


def test_rescue_repoints_schema_ofsmdm_to_ofserm(probes):
    # The folded-in schema fix: FN_G_TEST_CSTM mis-classifies OFSMDM but is
    # OFSERM. schema_for_function repoints g_schema, mirroring W164.
    probes.candidates = ["FN_G_TEST_CSTM"]
    probes.function_exists = True
    probes.function_schema = "OFSERM"
    qtype, term, schema = _bind(
        target_var="",
        obj_name="",
        raw_query="how does FN_G_TEST_CSTM work",
        g_schema="OFSMDM",  # wrong classifier default
    )
    assert qtype == "function"
    assert term == "FN_G_TEST_CSTM"
    assert schema == "OFSERM"  # REPOINTED from the OFSMDM input
    assert probes.calls["schema_for_function"] == ["FN_G_TEST_CSTM"]


def test_rescue_uses_first_candidate_convention(probes):
    # Mirrors the obj_name branch's candidates[0] convention.
    probes.candidates = ["FN_FIRST", "FN_SECOND"]
    probes.function_exists = True
    _, term, _ = _bind(raw_query="something FN_FIRST and FN_SECOND")
    assert term == "FN_FIRST"


def test_rescue_normalizes_to_canonical_key_form(probes):
    # A mixed-case extract must emit the canonical UPPER key form (so the
    # case-SENSITIVE resolve_function_nodes actually hits graph:<schema>:<FN>).
    probes.candidates = ["Fn_Mixed_Case"]
    probes.function_exists = True
    _, term, _ = _bind(raw_query="how does Fn_Mixed_Case work")
    assert term == "FN_MIXED_CASE"


# ---------------------------------------------------------------------------
# FP SAFETY — fall through to the unchanged whole-query-variable default
# ---------------------------------------------------------------------------


def test_rescue_no_candidate_falls_through_to_variable_xyzzy(probes):
    # "xyzzy function": extract_function_candidates returns [] live -> no
    # rescue, must stay variable on the raw query (W45 honest-decline downstream).
    probes.candidates = []
    qtype, term, schema = _bind(
        target_var="", obj_name="", raw_query="xyzzy function", g_schema="OFSMDM"
    )
    assert (qtype, term, schema) == ("variable", "xyzzy function", "OFSMDM")
    # No candidate -> the EXACT probe must never even run (short-circuit).
    assert probes.calls["function_exists_in_graph"] == []


def test_rescue_nonexact_candidate_falls_through_to_variable(probes):
    # The near-miss FP guard: a candidate that EXTRACTS but is NOT an exact
    # graph function. function_exists_in_graph returns False -> rescue must NOT
    # fire; fall through to whole-query-variable unchanged.
    probes.candidates = ["FN_LOAD_OPS_RISK_DAT"]  # typo / near-miss, no key
    probes.function_exists = False
    qtype, term, schema = _bind(
        target_var="",
        obj_name="",
        raw_query="How does FN_LOAD_OPS_RISK_DAT work?",
        g_schema="OFSMDM",
    )
    assert (qtype, term, schema) == (
        "variable",
        "How does FN_LOAD_OPS_RISK_DAT work?",
        "OFSMDM",
    )
    # It WAS probed (proving the gate is the exact check) and REJECTED.
    assert probes.calls["function_exists_in_graph"] == ["FN_LOAD_OPS_RISK_DAT"]
    # schema_for_function never reached on a non-exact candidate.
    assert probes.calls["schema_for_function"] == []


def test_rescue_never_calls_fuzzy_matcher(probes):
    # Belt-and-suspenders for the hard constraint: the fixture's
    # find_similar_function_names raises if called. An exact-hit run must
    # complete without touching it.
    probes.candidates = ["FN_LOAD_OPS_RISK_DATA"]
    probes.function_exists = True
    _bind(raw_query="How does FN_LOAD_OPS_RISK_DATA work?")
    assert probes.calls["find_similar_function_names"] == []


def test_genuine_variable_query_stays_variable_via_else(probes):
    # "What writes N_STD_ACCT_HEAD_AMT?" with target_var cleared: the live
    # extractor drops N_-prefixed column tokens -> candidates [] -> stays
    # variable on raw_query. (The production VARIABLE_TRACE path sets target_var
    # to the column and takes case (a); this models the (else) safety net.)
    probes.candidates = []
    qtype, term, schema = _bind(
        target_var="",
        obj_name="",
        raw_query="What writes N_STD_ACCT_HEAD_AMT?",
        g_schema="OFSERM",
    )
    assert (qtype, term, schema) == (
        "variable",
        "What writes N_STD_ACCT_HEAD_AMT?",
        "OFSERM",
    )
    assert probes.calls["function_exists_in_graph"] == []


# ---------------------------------------------------------------------------
# W164 DISJOINTNESS — rescue is in (else); never reached when target_var set
# ---------------------------------------------------------------------------


def test_w164_path_intact_rescue_not_reached_when_target_var_set(probes):
    # CS_Deferred_Tax shape: classifier ECHOED the name into target_variable,
    # so the W164 function-identity branch must win and the rescue must never
    # run. Distinguisher: W164 uses target_var verbatim; the rescue would use
    # candidates[0]. We set them to DIFFERENT values and assert the W164 value
    # wins AND extract_function_candidates was never called.
    probes.function_exists = True
    probes.function_schema = "OFSERM"
    probes.candidates = ["RESCUE_WOULD_PICK_THIS"]
    qtype, term, schema = _bind(
        target_var="CS_SOME_FN",
        obj_name="",
        column_owners=[],
        raw_query="how does CS_SOME_FN work",
        g_schema="OFSMDM",
    )
    assert (qtype, term, schema) == ("function", "CS_SOME_FN", "OFSERM")
    # The rescue's extractor must NEVER have run (W164 branch returned first).
    assert probes.calls["extract_function_candidates"] == []
    assert probes.calls["function_exists_in_graph"] == ["CS_SOME_FN"]


def test_case_d_unknown_target_var_still_variable_rescue_not_reached(probes):
    # target_var SET but neither column nor function (case d): must stay
    # byte-identical to pre-W164 — variable on target_var, rescue not reached
    # (rescue lives below, in the target_var-empty else).
    probes.function_exists = False
    probes.candidates = ["WOULD_BE_WRONG"]
    qtype, term, schema = _bind(
        target_var="CAP973", obj_name="", column_owners=[], g_schema="OFSMDM"
    )
    assert (qtype, term, schema) == ("variable", "CAP973", "OFSMDM")
    # extract is only called inside the (else) rescue (or the obj_name branch),
    # neither of which is reached here.
    assert probes.calls["extract_function_candidates"] == []
