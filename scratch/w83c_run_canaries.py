"""Run only the W83C-named integration tests, with UTF-8 IO."""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("t", "tests/integration/test_live_stream.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

for name, fn in m.TESTS:
    if "W83C" not in name:
        continue
    print(f"=== {name} ===")
    try:
        passed, extra = fn()
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {extra}")
    except Exception as exc:
        print(f"[FAIL] EXCEPTION: {exc}")
