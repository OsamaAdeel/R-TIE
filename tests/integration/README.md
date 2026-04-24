# Integration Tests

Manual smoke harness for RTIE's end-to-end behavior. Requires live
infrastructure and is run by hand when validating a change against a running
stack.

## Requirements

- RTIE backend running on `http://localhost:8000`
- Redis on `localhost:6379`

## Running

```
python tests/integration/test_live_stream.py
```

Exits 0 if all tests pass, 1 otherwise. A pass/fail table is printed for each
test plus a summary at the end.

## Not picked up by pytest

This directory is intentionally excluded from the default pytest discovery —
tests here require live services and would break CI if auto-collected. Run
the harness directly (as shown above) rather than via `pytest`.
