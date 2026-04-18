"""Unit tests for src.phase2.value_fetcher."""

import pytest

from src.phase2.value_fetcher import ValueFetcher


# -----------------------------------------------------------------------
# Minimal fakes -- no Oracle required.
# -----------------------------------------------------------------------

class _FakeSchemaTools:
    def __init__(self, rows):
        self._rows = rows
        self.last_sql = None
        self.last_params = None

    async def execute_raw(self, sql, params):
        self.last_sql = sql
        self.last_params = params
        return self._rows


class _FakeGuardian:
    """Guardian stub -- records calls and raises on demand."""

    def __init__(self, reject=False):
        self.reject = reject
        self.validate_calls = 0
        self.check_calls = 0

    def validate(self, sql):
        self.validate_calls += 1
        if self.reject:
            raise ValueError("rejected")
        return True

    def check_bind_variables(self, sql, params):
        self.check_calls += 1
        if self.reject:
            raise ValueError("rejected")
        return True


def _insert_node():
    return {
        "id": "FN_X_N1",
        "type": "INSERT",
        "target_table": "STG_OPS_RISK_DATA",
        "source_tables": ["ABL_OPS_RISK_DATA"],
        "column_maps": {
            "mapping": {"N_ANNUAL_GROSS_INCOME": "N_ANNUAL_GROSS_INCOME"},
        },
        "calculation": [],
        "conditions": [],
    }


@pytest.mark.asyncio
async def test_fetch_node_value_found():
    """Mock Oracle returns one row -- status is 'found' with row_count 1."""
    rows = [(1_000_000.0, "2025-12-31")]
    fetcher = ValueFetcher(_FakeSchemaTools(rows), _FakeGuardian())

    result = await fetcher.fetch_node_value(
        node=_insert_node(),
        filters={"mis_date": "2025-12-31", "account_number": "LD1"},
        target_column="N_ANNUAL_GROSS_INCOME",
    )
    assert result["status"] == "found"
    assert result["row_count"] == 1
    assert result["error"] is None


@pytest.mark.asyncio
async def test_fetch_node_value_empty():
    """No rows returned -> status='empty'."""
    fetcher = ValueFetcher(_FakeSchemaTools([]), _FakeGuardian())
    result = await fetcher.fetch_node_value(
        node=_insert_node(),
        filters={"mis_date": "2025-12-31"},
        target_column="N_ANNUAL_GROSS_INCOME",
    )
    assert result["status"] == "empty"
    assert result["row_count"] == 0


@pytest.mark.asyncio
async def test_fetch_node_value_uses_bind_variables():
    """The generated SQL must contain :mis_date, not the actual date string.
    bind_params must carry the value."""
    tools = _FakeSchemaTools([(500.0, "2025-12-31")])
    fetcher = ValueFetcher(tools, _FakeGuardian())
    result = await fetcher.fetch_node_value(
        node=_insert_node(),
        filters={"mis_date": "2025-12-31", "account_number": "LD1"},
        target_column="N_ANNUAL_GROSS_INCOME",
    )
    assert "2025-12-31" not in result["query"]
    assert ":mis_date" in result["query"]
    assert result["bind_params"]["mis_date"] == "2025-12-31"
    assert tools.last_params["mis_date"] == "2025-12-31"


@pytest.mark.asyncio
async def test_fetch_node_value_guardian_rejection():
    """If SQLGuardian rejects, execute_raw is never called and status='error'."""
    tools = _FakeSchemaTools([(1.0,)])
    guardian = _FakeGuardian(reject=True)
    fetcher = ValueFetcher(tools, guardian)

    result = await fetcher.fetch_node_value(
        node=_insert_node(),
        filters={"mis_date": "2025-12-31"},
        target_column="N_ANNUAL_GROSS_INCOME",
    )
    assert result["status"] == "error"
    assert "guardian" in (result["error"] or "").lower()
    assert tools.last_sql is None  # execute_raw never called


@pytest.mark.asyncio
async def test_detect_upstream_missing_empty_first_node():
    """First-position node with no rows is reported as upstream-missing."""
    fetcher = ValueFetcher(_FakeSchemaTools([]), _FakeGuardian())
    chain = [
        {
            "node": _insert_node(),
            "value_result": {"status": "empty", "rows": [], "row_count": 0,
                             "query": "SELECT 1 FROM X", "error": None},
        },
        {
            "node": _insert_node(),
            "value_result": {"status": "found", "rows": [{"N_ANNUAL_GROSS_INCOME": 100}],
                             "row_count": 1, "query": "", "error": None},
        },
    ]
    missing = fetcher.detect_upstream_missing(chain)
    assert missing is not None
    assert missing["position"] == 0
    assert missing["reason"] == "empty_source"


@pytest.mark.asyncio
async def test_detect_upstream_missing_none_when_all_found():
    """No missing upstream when every node has data."""
    fetcher = ValueFetcher(_FakeSchemaTools([]), _FakeGuardian())
    chain = [
        {"node": _insert_node(),
         "value_result": {"status": "found", "rows": [{"X": 1}], "row_count": 1,
                          "query": "", "error": None}},
    ]
    assert fetcher.detect_upstream_missing(chain) is None


@pytest.mark.asyncio
async def test_detect_upstream_missing_returns_error_node():
    """An error at any position is surfaced as upstream-missing."""
    fetcher = ValueFetcher(_FakeSchemaTools([]), _FakeGuardian())
    chain = [
        {"node": _insert_node(),
         "value_result": {"status": "error", "rows": [], "row_count": 0,
                          "query": "", "error": "ORA-12345"}},
    ]
    missing = fetcher.detect_upstream_missing(chain)
    assert missing is not None
    assert missing["reason"] == "error"
