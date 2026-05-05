# RTIE Makefile — convenience wrappers for the canary regression set.
#
# Targets shell out to the runner; they do NOT start the backend. Operator
# is expected to have `python run.py` running in another terminal before
# invoking `make canary-*`.
#
# Use `make` (GNU Make on Linux/macOS) or `mingw32-make` / `make` from Git
# Bash on Windows. PowerShell users without `make` can run the underlying
# python command directly.

.PHONY: canary-tier1 canary-tier2 canary-all canary-tier3-manual

canary-tier1:
	python tests/canary/run_canaries.py --tier 1

canary-tier2:
	python tests/canary/run_canaries.py --tier 2

canary-all:
	python tests/canary/run_canaries.py --tier 1 --tier 2

canary-tier3-manual:
	python tests/canary/run_canaries.py --tier 3
