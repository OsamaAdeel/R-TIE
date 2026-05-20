# W101 — Manifest Validator Relaxation (OFSAA N:M Semantics)

**Branch:** `fix/w101-manifest-validator-relaxation`
**Status:** Implemented; merged 2026-05-20
**Blocks:** Backend restart after Stage 1+2+3 corpus update
**Pattern parallel:** None directly — first validator-semantics correction in the manifest layer. Architectural sibling to W35 (schema as first-class) in that both move the parsing layer to match OFSAA's data model rather than ad-hoc Python intuitions.

---

## Diagnosis

Pre-restart verification of the Stage 1+2+3 corpus update (2026-05-20) showed `python run.py` would crash at loader startup. `load_manifest` raises `ManifestValidationError` from inside `load_all_functions` at [loader.py:226](../src/parsing/loader.py#L226) with no surrounding try/except, so any validator failure on the ABL_CAR_CSTM_V4 manifest aborts the backend before any function is parsed.

Two validator rules tripped on the live manifest:

### Rule 1 — order contiguity `[1..N]`

Original code at [manifest.py:573-585](../src/parsing/manifest.py#L573) (pre-W101):

```python
orders = [t.order for t in tasks]
expected = list(range(1, len(tasks) + 1))
if sorted(orders) != expected:
    raise ManifestValidationError(
        f"... non-contiguous task orders {sorted(orders)} (expected {expected})"
    )
```

The first container to fail: `ABL_CAPITAL_CONSOLIDATION_AND_PARAMETER_ASSIGNMENT` with orders `[2, 4, 5, 6, 8, 16]`.

### Rule 2 — global task-name uniqueness

Original code at [manifest.py:611-616](../src/parsing/manifest.py#L611) (pre-W101):

```python
if t.active:
    if name_u in seen_names:
        raise ManifestValidationError(
            f"... task name '{t.name}' appears in both "
            f"'{seen_names[name_u]}' and '{container_name}' — "
            f"task names must be globally unique"
        )
```

Three active task names appeared in two distinct sub-processes each, all referencing identical source files:

| Function | Container 1 (first seen) | Container 2 |
|---|---|---|
| `BNK_UNDERLYING_EXPOSURES_DATA_POPULATION` | `ABL_INVESTMENT_DATA_POPULATION > BNK_PRODUCT_PROCESSOR_DATA_POPULATION` (order 1) | `SEC_DATA_POPULATION` (order 2) |
| `INV_UNDERLYING_EXPOSURES_DATA_POPULATION` | `ABL_INVESTMENT_DATA_POPULATION > INV_PRODUCT_PROCESSOR_UNDERLYING_POP` (order 3) | `BNK_DATA_PROCESSING_STD` (order 1) |
| `FN_MITIGANT_ELIGIBILITY_CSTM` | `ABL Mitigant Processing - STD Approach - BIS V1 > Mitigant Collateral Eligibility Simple Approach - BIS` (order 9) | same parent > `ABL Mitigant Collateral Eligibility - Comprehensive Approach - BIS` (order 10) |

### Cross-corpus scan that decided the direction

To distinguish "manifest is wrong, fix the data" from "validator is wrong, fix the rules", a diagnostic pass swept all 120 task containers in `ABL_CAR_CSTM_V4/manifest.yaml`:

| Container category | Count |
|---|---:|
| Clean (all orders dense 1..N, active orders dense) | 67 |
| **All orders dense, active orders have gaps** (runchart-absolute-position with inactive placeholders preserved) | **14** |
| **All orders non-contiguous** (validator FAIL) | **39** |

The 14 "coincidentally-passing" containers prove the runchart-absolute-position semantic is the manifest's de facto authoring convention. The 39 failing containers are the SAME convention with the inactive placeholder rows also pruned. Renumbering 39 containers to `[1..N]` would lose runchart row-number traceability across 53 containers (~44% of the corpus) and contradict the Cowork manifest authoring spec.

For the duplicate-name cases: structural signals (identical source_file, identical descriptions, different orders, distinct hierarchy paths) all match OFSAA's "same function fires in N process contexts" pattern — not a Stage 2 duplication bug. An exhaustive scan confirmed only 3 such cases corpus-wide; if it were a copy-paste bug it would have produced many more.

**Conclusion:** the validator's two rules embed Python-collection intuitions (orders should be dense; names should be unique) that don't match OFSAA's semantics (orders are absolute row positions with gaps; function-task is N:M).

---

## Design

Two narrowly-scoped relaxations in `_validate_and_index` ([src/parsing/manifest.py](../src/parsing/manifest.py)). No new module-level constants, no public API change, no manifest data edits.

### Change 1 — order check

```python
orders = [t.order for t in tasks]
if len(orders) != len(set(orders)):
    raise ManifestValidationError(
        f"... duplicate task 'order' integers {sorted(orders)}"
    )
active_orders = sorted(t.order for t in tasks if t.active)
if active_orders and active_orders != list(
    range(active_orders[0], active_orders[0] + len(active_orders))
):
    logger.debug(
        "Manifest: %s '%s' active task orders %s have gaps "
        "(runchart-absolute-position convention, W101).",
        container_label, container_name, active_orders,
    )
```

Uniqueness within container is preserved (real bug case: two tasks at the same `order` is a genuine authoring mistake). Contiguity is dropped. A debug-level log fires when the active subset has gaps so the runchart-absolute-position convention stays auditable from logs alone.

### Change 2 — global name uniqueness

Track first-seen `(container, source_file_base)` per active task name. Subsequent occurrences:

- **Same source_file** → allowed (OFSAA N:M); debug-log the duplication so it's auditable.
- **Different source_file** → still raised — that's a real function-name collision worth catching.

The within-container active-name check at line 587-600 was updated in the same spirit (source-aware), so error messages stay local to where the collision is detected.

### Change 3 — file-index collision

The `_file_index` insertion previously raised when two tasks referenced the same `source_file`. Under W101's N:M semantics, that's exactly what happens for the 3 in-tree cases. Relaxed to debug-log and keep the first binding so `get_task_by_file` remains deterministic.

---

## Tests

Four W101 tests added to [tests/unit/parsing/test_manifest.py](../tests/unit/parsing/test_manifest.py):

| Test | Asserts |
|---|---|
| `test_non_contiguous_orders_pass_w101` | Container with `[2, 4, 5, 6, 8, 16]` validates clean; declaration order preserved |
| `test_duplicate_orders_within_container_raise_w101` | Two tasks at the same `order=1` still rejected (real bug case) |
| `test_same_name_same_source_across_containers_passes_w101` | OFSAA N:M happy path — two containers, same active name, same source_file |
| `test_same_name_different_source_across_containers_raises_w101` | Real name collision — same active name across containers but different source_file — still rejected |

The pre-existing `test_non_contiguous_order_raises` was retired (its assertion was the validator behavior W101 explicitly reverses).
The pre-existing `test_duplicate_task_names_raise` still passes — the new error message preserves the substring "duplicate task name".

All 18 manifest tests green.

---

## What W101 does NOT do

- **Does not edit any manifest data.** All 39 non-contiguous containers and 3 N:M duplicates are valid per the new rules; nothing about the live manifest needs to change.
- **Does not fix the `"FN PRODUCT RECLASS CSTM"` (spaces) vs `FN_PRODUCT_RECLASS_CSTM.sql` (underscores) name mismatch on line 1340.** That's a pre-existing Stage-3 authoring bug surfaced now that the contiguity gate no longer fires first. Tracked as a separate Stage-3 cleanup item.
- **Does not change the loader, indexer, or any consumer of `BatchManifest`.** The dataclass shape and `iter_*` / `get_task*` API are unchanged.
- **Does not log at info or warn level for the new "allowed N:M" / "allowed gap" cases.** Only debug-level. The convention is the spec; logging it at warn would be noise.

---

## Files changed

| File | Change |
|---|---|
| [src/parsing/manifest.py](../src/parsing/manifest.py) | `_validate_and_index` relaxed per Changes 1-3 above; docstring updated to reference W101 |
| [tests/unit/parsing/test_manifest.py](../tests/unit/parsing/test_manifest.py) | Retired `test_non_contiguous_order_raises`; added 4 W101 tests |
| [docs/RTIE_Weakness_Log.md](RTIE_Weakness_Log.md) | W101 entry appended |
| [docs/w101_manifest_validator_relaxation.md](w101_manifest_validator_relaxation.md) | This document |

---

## Verification

After merge, the pre-restart verification script (`tmp_verify_stage3.py`) re-run reports both W101 blockers as cleared. A third manifest issue (the `FN PRODUCT RECLASS CSTM` name mismatch) surfaces post-W101 — flagged for separate decision before restart.
