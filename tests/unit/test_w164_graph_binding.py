"""W164 — offline unit tests for :func:`src.main.resolve_graph_binding`.

The graph-pipeline binding head used to bind ANY non-empty
``target_variable`` to a column ("variable"), which mis-routed a
classifier-echoed FUNCTION name into a column lookup (-> [] -> raw-source
fallback) and wasted a correctly-resolved ``object_name``. W164 carves out the
known-function sub-case via IDENTITY (column-first, function-second) while
keeping the known-column (a) and genuinely-unknown (d) cases byte-identical to
the pre-W164 ``if target_var: -> variable`` for EVERY input.

These tests pin the branch-selection contract (the W164 discriminator matrix).
The identity primitives (``function_exists_in_graph`` / ``schema_for_function``
/ ``extract_function_candidates``) are monkeypatched at the ``main`` module
namespace so no fake Redis is needed; ``column_owners`` is passed in directly
(the helper never probes it — the caller hoists the single
``schemas_for_column`` read). Branch firing is asserted via call-tracking, not
just the output tuple, so case (c)-with-fn-echo can be proven to win via the
function-identity branch and NOT via the ``elif obj_name`` (W43) branch.

Live end-to-end behavior (graph payload, fan-in shape, badge) is covered by the
W164 discriminator matrix against a running backend.
"""

from __future__ import annotations

from typing import List

import pytest

from src import main as main_mod


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _SentinelRedis:
    """Non-None identity stand-in. The primitives are monkeypatched, so no
    method is ever called on it — it only has to be a truthy object."""


@pytest.fixture
def probes(monkeypatch):
    """Monkeypatch the three identity primitives and record their calls.

    Returns a mutable config object the test tweaks before calling the helper,
    plus ``calls`` lists so the test can assert WHICH branch fired (e.g. that
    the W43 ``extract_function_candidates`` path was NOT taken when the
    function-identity branch should win).
    """

    class _Cfg:
        def __init__(self):
            self.function_exists = False
            self.function_schema = None  # schema_for_function return
            self.candidates: List[str] = []  # extract_function_candidates return
            self.calls = {
                "function_exists_in_graph": [],
                "schema_for_function": [],
                "extract_function_candidates": [],
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

    monkeypatch.setattr(main_mod, "function_exists_in_graph", _fake_function_exists)
    monkeypatch.setattr(main_mod, "schema_for_function", _fake_schema_for_function)
    monkeypatch.setattr(main_mod, "extract_function_candidates", _fake_extract)
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
# (a) known column -> variable (byte-identical to pre-W164)
# ---------------------------------------------------------------------------


def test_case_a_known_column_binds_variable(probes):
    qtype, term, schema = _bind(
        target_var="N_EOP_BAL", column_owners=["OFSMDM"], g_schema="OFSMDM"
    )
    assert (qtype, term, schema) == ("variable", "N_EOP_BAL", "OFSMDM")


def test_case_a_column_first_short_circuits_function_probe(probes):
    # column_owners non-empty -> the function probe must never run (column-first).
    probes.function_exists = True  # would matter only if probed
    _bind(target_var="N_EOP_BAL", column_owners=["OFSMDM"])
    assert probes.calls["function_exists_in_graph"] == []
    assert probes.calls["schema_for_function"] == []


def test_case_a_known_column_ignores_obj_name(probes):
    # A known column must bind variable even when obj_name is also set.
    qtype, term, _ = _bind(
        target_var="N_EOP_BAL", column_owners=["OFSERM"], obj_name="some blob"
    )
    assert (qtype, term) == ("variable", "N_EOP_BAL")
    assert probes.calls["extract_function_candidates"] == []


# ---------------------------------------------------------------------------
# (b/c) known function -> function (THE FIX) + cross-schema resolve
# ---------------------------------------------------------------------------


def test_case_b_known_function_binds_function(probes):
    probes.function_exists = True
    probes.function_schema = "OFSERM"
    qtype, term, schema = _bind(
        target_var="CS_NET_AT1_CAPITAL_POST_REGULATORY_ADJUSTMENT",
        column_owners=[],
        g_schema="OFSMDM",
    )
    assert qtype == "function"
    assert term == "CS_NET_AT1_CAPITAL_POST_REGULATORY_ADJUSTMENT"
    # W164 tracked second change: cross-schema resolve re-points g_schema.
    assert schema == "OFSERM"


def test_case_b_normalizes_mixed_case_to_canonical_key_form(probes):
    # The crux of the casing fix: a mixed-case classifier echo must be emitted
    # as the canonical UPPER Redis-key form so resolve_function_nodes ->
    # get_function_graph (case-SENSITIVE) actually hits graph:<schema>:<FN>.
    # Without this, function-binding + cross-schema repoint are inert (0 nodes
    # -> raw-source fallback, same as pre-W164).
    probes.function_exists = True
    probes.function_schema = "OFSERM"
    qtype, term, schema = _bind(
        target_var="CS_Net_AT1_Capital_Post_Regulatory_Adjustment",
        column_owners=[],
        g_schema="OFSMDM",
    )
    assert qtype == "function"
    assert term == "CS_NET_AT1_CAPITAL_POST_REGULATORY_ADJUSTMENT"
    assert schema == "OFSERM"


def test_case_b_normalizes_internal_whitespace(probes):
    # normalize_function_name collapses whitespace runs to a single underscore
    # then uppercases (matches the loader's key-writing rule).
    probes.function_exists = True
    _, term, _ = _bind(target_var="CS  Net AT1", column_owners=[])
    assert term == "CS_NET_AT1"


def test_case_b_function_schema_none_keeps_orchestrator_default(probes):
    probes.function_exists = True
    probes.function_schema = None  # resolver can't pin a schema
    qtype, term, schema = _bind(
        target_var="CS_SOME_FN", column_owners=[], g_schema="OFSMDM"
    )
    assert (qtype, term, schema) == ("function", "CS_SOME_FN", "OFSMDM")


def test_case_b_does_not_consult_obj_name_or_w43(probes):
    # The function-identity branch must use target_var verbatim, never the
    # W43 extract_function_candidates(raw_query) path.
    probes.function_exists = True
    probes.candidates = ["WRONG_FN_FROM_RAW"]
    _, term, _ = _bind(
        target_var="CS_SOME_FN", column_owners=[], obj_name="blob", raw_query="how does CS_SOME_FN work"
    )
    assert term == "CS_SOME_FN"
    assert probes.calls["extract_function_candidates"] == []


# ---------------------------------------------------------------------------
# (c) PRECEDENCE — fn echoed into target_var AND obj_name set: function wins
# ---------------------------------------------------------------------------


def test_case_c_fn_echo_precedence_function_identity_wins(probes):
    # W76 fired (obj_name set) AND the classifier echoed the fn name into
    # target_variable. The FUNCTION-IDENTITY branch must win — NOT elif obj_name.
    probes.function_exists = True
    probes.function_schema = "OFSERM"
    probes.candidates = ["SHOULD_NOT_BE_USED"]
    qtype, term, schema = _bind(
        target_var="CS_SOME_FN",
        obj_name="enriched semantic blob",
        column_owners=[],
        g_schema="OFSMDM",
    )
    assert (qtype, term, schema) == ("function", "CS_SOME_FN", "OFSERM")
    # Proof the W43 (obj_name) branch did NOT fire.
    assert probes.calls["extract_function_candidates"] == []
    # Proof the function-identity branch DID fire.
    assert probes.calls["function_exists_in_graph"] == ["CS_SOME_FN"]


# ---------------------------------------------------------------------------
# (3) obj_name only (target_var empty/cleared) -> W43 function path (unchanged)
# ---------------------------------------------------------------------------


def test_case_obj_name_only_uses_w43_candidate(probes):
    probes.candidates = ["FN_RESOLVED"]
    qtype, term, schema = _bind(
        target_var="", obj_name="enriched blob", raw_query="trace foo", g_schema="OFSMDM"
    )
    assert (qtype, term, schema) == ("function", "FN_RESOLVED", "OFSMDM")
    assert probes.calls["extract_function_candidates"] == ["trace foo"]


def test_case_obj_name_only_falls_back_to_obj_name_when_no_candidate(probes):
    probes.candidates = []  # extract found nothing
    qtype, term, _ = _bind(target_var="", obj_name="OFSMDM.SOME_BLOB")
    assert (qtype, term) == ("function", "OFSMDM.SOME_BLOB")


# ---------------------------------------------------------------------------
# (d) unknown target_var -> variable -> [] -> fallback (byte-identical)
# ---------------------------------------------------------------------------


def test_case_d_unknown_target_binds_variable(probes):
    probes.function_exists = False  # not a function either
    qtype, term, schema = _bind(
        target_var="CAP973", column_owners=[], g_schema="OFSMDM"
    )
    assert (qtype, term, schema) == ("variable", "CAP973", "OFSMDM")


def test_case_d_unknown_target_with_obj_name_still_variable(probes):
    """The non-negotiable W164 gate: an unknown target_var must bind variable
    EVEN WHEN obj_name is also set. The nesting (target_var outer) guarantees
    this; a flat elif-obj_name-first chain would wrongly route it to W43."""
    probes.function_exists = False
    probes.candidates = ["WOULD_BE_WRONG"]
    qtype, term, schema = _bind(
        target_var="CAP973",
        obj_name="enriched semantic blob",
        column_owners=[],
        g_schema="OFSMDM",
    )
    assert (qtype, term, schema) == ("variable", "CAP973", "OFSMDM")
    # The W43 obj_name path must NOT have fired.
    assert probes.calls["extract_function_candidates"] == []


# ---------------------------------------------------------------------------
# (else) neither target_var nor obj_name -> variable on raw_query (unchanged)
# ---------------------------------------------------------------------------


def test_case_else_binds_variable_on_raw_query(probes):
    qtype, term, schema = _bind(
        target_var="", obj_name="", raw_query="what is the meaning of life", g_schema="OFSMDM"
    )
    assert (qtype, term, schema) == ("variable", "what is the meaning of life", "OFSMDM")


# ---------------------------------------------------------------------------
# Column-first precedence when a name matches BOTH a column and a function
# ---------------------------------------------------------------------------


def test_both_column_and_function_routes_to_variable(probes):
    # column_owners non-empty AND the name also exists as a function: the
    # W159-safe default (variable) must win.
    probes.function_exists = True
    probes.function_schema = "OFSERM"
    qtype, term, schema = _bind(
        target_var="AMBIGUOUS_NAME", column_owners=["OFSMDM"], g_schema="OFSMDM"
    )
    assert (qtype, term, schema) == ("variable", "AMBIGUOUS_NAME", "OFSMDM")
    # Function branch never consulted.
    assert probes.calls["function_exists_in_graph"] == []
    assert probes.calls["schema_for_function"] == []
