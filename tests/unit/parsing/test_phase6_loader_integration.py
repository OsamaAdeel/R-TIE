"""W35 Phase 6 — loader integration test.

Verifies that ``load_all_functions``:
  - extracts derivations from on-disk source,
  - attaches them to the per-function graph dict (msgpack-encoded under
    ``graph:<schema>:<fn>``), and
  - cross-references each case_when_target literal-index record with a
    compact derivation summary at ``graph:literal:<schema>:<id>``.

Uses a temp directory with one .sql fixture so the test is self-
contained and independent of the real db/modules/ tree.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.parsing.serializer import from_msgpack


# Distilled from CS_Deferred_Tax_Asset_Net_of_DTL_Calculation: a single
# MERGE block, one COND alias, one Pattern-A EXP, one fallback EXP,
# WHEN MATCHED routing.
_FIXTURE = """\
CREATE OR REPLACE FUNCTION OFSERM.DERIV_FIXTURE RETURN VARCHAR2 AS
BEGIN
    MERGE INTO FCT_STANDARD_ACCT_HEAD TT USING (
      SELECT TT.N_RUN_SKEY,
        MIN(CASE WHEN ((DIM.V_STD_ACCT_HEAD_ID = 'CAP943')) THEN 10 ELSE 11 END) AS COND_777_10,
        (MAX(CASE WHEN CAPITAL_ACCOUNTING.n_std_acct_head_skey IN
              (SELECT n_std_acct_head_skey FROM DIM WHERE v_std_acct_head_id = 'CAP309')
              THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END )
         - MAX(CASE WHEN CAPITAL_ACCOUNTING.n_std_acct_head_skey IN
              (SELECT n_std_acct_head_skey FROM DIM WHERE v_std_acct_head_id = 'CAP863')
              THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END )) AS EXP_777_10,
        MIN(FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT) AS EXP_777_11
      FROM FCT_STANDARD_ACCT_HEAD
      WHERE ((DIM.V_STD_ACCT_HEAD_ID = 'CAP943'))
    ) SS ON (TT.id = SS.id)
    WHEN MATCHED THEN UPDATE SET
      TT.N_STD_ACCT_HEAD_AMT = CASE WHEN COND_777_10=10 THEN EXP_777_10 ELSE EXP_777_11 END;
    COMMIT;
    RETURN 'OK';
END;
/
"""


def _make_fixture_dir(tmp_path: Path) -> str:
    fn_path = tmp_path / "DERIV_FIXTURE.sql"
    fn_path.write_text(_FIXTURE, encoding="utf-8")
    return str(tmp_path)


def _make_redis_mock() -> tuple[dict[str, bytes], MagicMock]:
    storage: dict[str, bytes] = {}
    mock = MagicMock()
    mock.set.side_effect = lambda k, v: storage.update({k: v})
    mock.get.side_effect = lambda k: storage.get(k)
    return storage, mock


def test_loader_writes_derivations_field_on_function_graph(tmp_path):
    fixture_dir = _make_fixture_dir(tmp_path)
    storage, mock_redis = _make_redis_mock()

    from src.parsing.loader import load_all_functions

    result = load_all_functions(
        functions_dir=fixture_dir,
        schema="OFSERM",
        redis_client=mock_redis,
        force_reparse=True,
    )

    assert result["functions_parsed"] == 1
    assert result["derivations_indexed"] == 1

    graph_blob = storage["graph:OFSERM:DERIV_FIXTURE"]
    graph = from_msgpack(graph_blob)
    assert "derivations" in graph
    assert len(graph["derivations"]) == 1
    d = graph["derivations"][0]
    assert d["target_literal"] == "CAP943"
    assert d["operation"] == "SUBTRACT"
    assert d["source_literals"] == ["CAP309", "CAP863"]
    assert d["target_column"] == "N_STD_ACCT_HEAD_AMT"
    assert d["function"] == "DERIV_FIXTURE"
    assert "line_range" in d
    assert len(d["operands"]) == 2


def test_loader_cross_references_into_literal_index(tmp_path):
    fixture_dir = _make_fixture_dir(tmp_path)
    storage, mock_redis = _make_redis_mock()

    from src.parsing.loader import load_all_functions

    load_all_functions(
        functions_dir=fixture_dir,
        schema="OFSERM",
        redis_client=mock_redis,
        force_reparse=True,
    )

    # CAP943 literal index should now include a derivation reference
    # on its case_when_target record.
    cap943_blob = storage["graph:literal:OFSERM:CAP943"]
    cap943_records = from_msgpack(cap943_blob)
    target_records = [
        r for r in cap943_records
        if r.get("role") == "case_when_target"
        and r.get("function") == "DERIV_FIXTURE"
    ]
    assert len(target_records) == 1
    rec = target_records[0]
    assert "derivation" in rec
    assert rec["derivation"]["operation"] == "SUBTRACT"
    assert rec["derivation"]["source_literals"] == ["CAP309", "CAP863"]
    assert rec["derivation"]["target_column"] == "N_STD_ACCT_HEAD_AMT"

    # Source literals (CAP309, CAP863) should NOT have a derivation
    # field on their records — they're operands, not targets.
    cap309_records = from_msgpack(storage["graph:literal:OFSERM:CAP309"])
    for r in cap309_records:
        assert "derivation" not in r


def test_loader_summary_includes_derivations_indexed_count(tmp_path):
    fixture_dir = _make_fixture_dir(tmp_path)
    _, mock_redis = _make_redis_mock()

    from src.parsing.loader import load_all_functions

    result = load_all_functions(
        functions_dir=fixture_dir,
        schema="OFSERM",
        redis_client=mock_redis,
        force_reparse=True,
    )
    assert "derivations_indexed" in result
    assert isinstance(result["derivations_indexed"], int)
    assert result["derivations_indexed"] >= 1


def test_loader_no_derivations_when_no_pattern(tmp_path):
    """A function with no CAP-coded MERGE template emits no derivations
    (graph dict gets no derivations field, summary count is 0)."""
    fn_path = tmp_path / "PLAIN_FN.sql"
    fn_path.write_text(
        """\
CREATE OR REPLACE FUNCTION OFSERM.PLAIN_FN RETURN VARCHAR2 AS
BEGIN
    INSERT INTO TGT (col) VALUES (1);
    RETURN 'OK';
END;
/
""",
        encoding="utf-8",
    )
    _, mock_redis = _make_redis_mock()

    from src.parsing.loader import load_all_functions

    result = load_all_functions(
        functions_dir=str(tmp_path),
        schema="OFSERM",
        redis_client=mock_redis,
        force_reparse=True,
    )
    assert result["derivations_indexed"] == 0
