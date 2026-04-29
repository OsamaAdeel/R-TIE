"""W35 Phase 5 — loader integration test.

Verifies that load_all_functions extracts business-identifier literals
from on-disk source and persists them at graph:literal:<schema>:<id>.

Uses a temp directory with a single .sql fixture so the test is
self-contained and independent of the real db/modules/ tree.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.parsing.serializer import from_msgpack


_FIXTURE = """\
CREATE OR REPLACE FUNCTION OFSERM.FIXTURE_FN RETURN VARCHAR2 AS
BEGIN
    INSERT INTO TGT (col)
      SELECT x FROM SRC
      WHERE V_STD_ACCT_HEAD_ID IN ('CAP139', 'CAP943', 'CAP973');
    MERGE INTO TGT TT USING (
      SELECT MIN(CASE WHEN ((DIM.V_STD_ACCT_HEAD_ID = 'CAP943')) THEN 10 ELSE 11 END) AS COND_X,
        (MAX(CASE WHEN CAPITAL_ACCOUNTING.n_std_acct_head_skey IN
          (SELECT n_std_acct_head_skey FROM DIM WHERE v_std_acct_head_id = 'CAP309')
          THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END)) AS EXP_X
      FROM SRC
      WHERE ((DIM.V_STD_ACCT_HEAD_ID = 'CAP943'))
    ) SS ON (TT.id = SS.id) WHEN MATCHED THEN UPDATE SET col = EXP_X;
    COMMIT;
    RETURN 'OK';
END;
/
"""


def _make_tmp_function(tmp_path: Path) -> str:
    fn_path = tmp_path / "FIXTURE_FN.sql"
    fn_path.write_text(_FIXTURE, encoding="utf-8")
    return str(tmp_path)


def test_loader_writes_literal_index(tmp_path):
    """load_all_functions builds the literal index and writes one Redis
    SET per identifier under graph:literal:<schema>:<id>."""
    fixture_dir = _make_tmp_function(tmp_path)

    storage: dict[str, bytes] = {}
    mock_redis = MagicMock()
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})
    mock_redis.get.side_effect = lambda k: storage.get(k)

    from src.parsing.loader import load_all_functions

    result = load_all_functions(
        functions_dir=fixture_dir,
        schema="OFSERM",
        redis_client=mock_redis,
        force_reparse=True,
    )

    assert result["functions_parsed"] == 1
    assert result["literals_indexed"] >= 1

    # Inspect what got written.
    written_literal_keys = [k for k in storage if k.startswith("graph:literal:")]
    assert "graph:literal:OFSERM:CAP943" in written_literal_keys
    assert "graph:literal:OFSERM:CAP139" in written_literal_keys
    assert "graph:literal:OFSERM:CAP973" in written_literal_keys
    assert "graph:literal:OFSERM:CAP309" in written_literal_keys

    # Decode CAP943 and verify it has BOTH case_when_target (computer
    # signal) AND in_list_member (loader signal). This is the core
    # Phase 5 verification: the index distinguishes loader from computer.
    cap943_records = from_msgpack(storage["graph:literal:OFSERM:CAP943"])
    roles = {r["role"] for r in cap943_records}
    assert "case_when_target" in roles
    assert "in_list_member" in roles


def test_loader_summary_includes_literals_indexed_count(tmp_path):
    """The result dict gains a literals_indexed key (Phase 5 contract)."""
    fixture_dir = _make_tmp_function(tmp_path)

    storage: dict[str, bytes] = {}
    mock_redis = MagicMock()
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})
    mock_redis.get.side_effect = lambda k: storage.get(k)

    from src.parsing.loader import load_all_functions

    result = load_all_functions(
        functions_dir=fixture_dir,
        schema="OFSERM",
        redis_client=mock_redis,
        force_reparse=True,
    )

    assert "literals_indexed" in result
    assert isinstance(result["literals_indexed"], int)
    assert result["literals_indexed"] >= 4  # CAP139, CAP943, CAP973, CAP309


def test_loader_disables_indexing_with_empty_pattern_dict(tmp_path):
    """Passing an empty pattern dict that compiles to empty disables
    indexing — but compile_patterns falls back to default on empty dict.

    This test pins the actual behaviour: passing an explicitly empty
    config still extracts via the default CAP\\d{3} pattern (default
    fallback is the documented contract). Callers wanting to disable
    must omit the key entirely from settings OR pass a pattern entry
    that matches nothing — explicit "disable" is not a Phase 5 feature.
    """
    fixture_dir = _make_tmp_function(tmp_path)

    storage: dict[str, bytes] = {}
    mock_redis = MagicMock()
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})
    mock_redis.get.side_effect = lambda k: storage.get(k)

    from src.parsing.loader import load_all_functions

    result = load_all_functions(
        functions_dir=fixture_dir,
        schema="OFSERM",
        redis_client=mock_redis,
        force_reparse=True,
        business_identifier_patterns={},  # falls back to default
    )

    # Default pattern still applied → literal index built.
    assert result["literals_indexed"] >= 1


def test_loader_custom_pattern_extracts_only_matching_codes(tmp_path):
    """Supplying a narrower custom pattern restricts what gets indexed."""
    fixture_dir = _make_tmp_function(tmp_path)

    storage: dict[str, bytes] = {}
    mock_redis = MagicMock()
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})
    mock_redis.get.side_effect = lambda k: storage.get(k)

    from src.parsing.loader import load_all_functions

    # Only CAP9XX codes — should still match CAP943 / CAP973 but skip
    # CAP139 and CAP309.
    custom = {
        "cap9xx_only": {
            "regex": r"CAP9\d{2}",
            "description": "narrow CAP9XX only",
        },
    }
    load_all_functions(
        functions_dir=fixture_dir,
        schema="OFSERM",
        redis_client=mock_redis,
        force_reparse=True,
        business_identifier_patterns=custom,
    )

    assert "graph:literal:OFSERM:CAP943" in storage
    assert "graph:literal:OFSERM:CAP973" in storage
    # The narrower pattern excludes these:
    assert "graph:literal:OFSERM:CAP139" not in storage
    assert "graph:literal:OFSERM:CAP309" not in storage
