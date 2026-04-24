#!/usr/bin/env python3
"""
extract_t2t_sql.py
------------------
Extracts T2T SQL from OFSAA TRANSFORM DATA log files.

Usage:
    python extract_t2t_sql.py <logs_folder> <output_folder>

Example:
    python extract_t2t_sql.py "C:/Data/TRANSFORM DATA" "C:/RTIE/db/modules/ABL_BIS_CAPITAL_STRUCTURE/functions"

For each job group in the logs folder:
  - Reads the _T2TCPP file
  - Extracts task name from ETLLoadHistory insert or V_RULE_CODE line
  - Extracts INSERT SQL from "insert query for load data formulated =" line
  - Writes <task_name>.sql to output folder

CSTM DT tasks are NOT handled here — pull those from Oracle ALL_SOURCE separately.
"""

import os
import re
import sys
from pathlib import Path
from collections import defaultdict


# ── Patterns ──────────────────────────────────────────────────────────────────

# Task name: from ETLLoadHistory insert — most reliable, appears at end of file
# Example: INSERT into ETLLoadHistory values(...,'STD_ACCT_HEAD_THRESHOLD_TREATMENT_DATA_POP',...)
RE_TASK_NAME_ETL = re.compile(
    r"INSERT into ETLLoadHistory values\([^)]*?'([A-Za-z0-9_]+)'\s*,\s*to_date",
    re.IGNORECASE
)

# Task name: fallback from V_RULE_CODE filter line
# Example: V_RULE_CODE = 'STD_ACCT_HEAD_THRESHOLD_TREATMENT_DATA_POP'
RE_TASK_NAME_RULE = re.compile(
    r"V_RULE_CODE\s*=\s*'([A-Za-z0-9_]+)'",
    re.IGNORECASE
)

# SQL: insert query line
# Example: insert query for load data formulated =insert /*+APPEND*/ into ...
RE_SQL = re.compile(
    r"insert query for load data formulated\s*=\s*(insert\s.+)",
    re.IGNORECASE | re.DOTALL
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def find_t2tcpp_files(logs_folder: Path) -> list[Path]:
    """Find all _T2TCPP log files (case-insensitive) in the folder."""
    matches = []
    for f in logs_folder.iterdir():
        if f.is_file() and "T2TCPP" in f.name.upper():
            matches.append(f)
    return sorted(matches)


def extract_task_name(content: str) -> str | None:
    """Extract task name from log content. ETLLoadHistory is preferred."""
    m = RE_TASK_NAME_ETL.search(content)
    if m:
        return m.group(1).strip()
    m = RE_TASK_NAME_RULE.search(content)
    if m:
        return m.group(1).strip()
    return None


def extract_sql(content: str) -> str | None:
    """Extract the INSERT SQL from the log line."""
    m = RE_SQL.search(content)
    if not m:
        return None
    sql = m.group(1).strip()
    # The SQL runs to end of line — trim any trailing log noise
    # (the line is long but single-line in the log)
    # Strip trailing whitespace and log suffixes if any
    sql = sql.split("\n")[0].strip()
    return sql


def sanitize_filename(name: str) -> str:
    """Ensure filename only contains [A-Za-z0-9_]."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 3:
        print("Usage: python extract_t2t_sql.py <logs_folder> <output_folder>")
        sys.exit(1)

    logs_folder = Path(sys.argv[1])
    output_folder = Path(sys.argv[2])

    if not logs_folder.exists():
        print(f"ERROR: Logs folder not found: {logs_folder}")
        sys.exit(1)

    output_folder.mkdir(parents=True, exist_ok=True)

    t2tcpp_files = find_t2tcpp_files(logs_folder)

    if not t2tcpp_files:
        print(f"No _T2TCPP files found in: {logs_folder}")
        sys.exit(1)

    print(f"Found {len(t2tcpp_files)} T2TCPP log files.")
    print(f"Output folder: {output_folder}\n")

    success = 0
    skipped_no_task = []
    skipped_no_sql = []
    skipped_duplicate = []
    seen_tasks = {}

    for log_file in t2tcpp_files:
        content = log_file.read_text(encoding="utf-8", errors="replace")

        task_name = extract_task_name(content)
        if not task_name:
            skipped_no_task.append(log_file.name)
            continue

        sql = extract_sql(content)
        if not sql:
            skipped_no_sql.append((log_file.name, task_name))
            continue

        safe_name = sanitize_filename(task_name)
        if safe_name != task_name:
            print(f"  WARNING: task name sanitized: '{task_name}' -> '{safe_name}'")

        # Duplicate check
        if safe_name in seen_tasks:
            skipped_duplicate.append((log_file.name, safe_name, seen_tasks[safe_name]))
            continue

        seen_tasks[safe_name] = log_file.name
        out_path = output_folder / f"{safe_name}.sql"
        out_path.write_text(sql + "\n", encoding="utf-8")
        print(f"  OK  {safe_name}.sql")
        success += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"  Extracted:          {success}")
    print(f"  Skipped (no task):  {len(skipped_no_task)}")
    print(f"  Skipped (no SQL):   {len(skipped_no_sql)}")
    print(f"  Skipped (dup):      {len(skipped_duplicate)}")

    if skipped_no_task:
        print(f"\nFiles where task name could not be found:")
        for f in skipped_no_task:
            print(f"  {f}")

    if skipped_no_sql:
        print(f"\nFiles where SQL could not be found (task known):")
        for f, t in skipped_no_sql:
            print(f"  {f}  (task: {t})")

    if skipped_duplicate:
        print(f"\nDuplicate task names (second occurrence skipped):")
        for f, t, first in skipped_duplicate:
            print(f"  {f}  (task: {t}, first seen in: {first})")

    print(f"\nNext step: run wrap_ofsaa_tasks.py on {output_folder}")


if __name__ == "__main__":
    main()