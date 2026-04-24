#!/usr/bin/env python3
"""
extract_type3_sql.py
--------------------
Extracts TYPE3 rule SQL from OFSAA RULE_EXECUTION log files.

Usage:
    python extract_type3_sql.py <logs_folder> <excel_runchart> <output_folder>

Example:
    python extract_type3_sql.py "C:/Data/RULE_EXECUTION" "C:/Data/New_Microsoft_Excel_Worksheet.xlsx" "C:/RTIE/db/modules/ABL_BIS_CAPITAL_STRUCTURE/functions"

For each log file in RULE_EXECUTION:
  - Extracts Rule ID
  - Looks up task name from Excel run chart using Rule ID as key
  - Extracts FINAL QUERY (MERGE SQL)
  - Writes <task_name>.sql to output folder
"""

import os
import re
import sys
from pathlib import Path
from collections import defaultdict

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas not installed. Run: pip install pandas xlrd")
    sys.exit(1)


# ── Patterns ──────────────────────────────────────────────────────────────────

RE_RULE_CODE = re.compile(r'Rule Code\s*:::\s*(RLBL\w+)', re.IGNORECASE)
RE_FINAL_QUERY = re.compile(r'FINAL QUERY\s*:::\s*(MERGE\s.+)', re.IGNORECASE | re.DOTALL)


# ── Excel lookup: rlbl_code -> task_name ─────────────────────────────────────

def build_ruleid_map(excel_path: Path) -> dict:
    """
    Read the run chart XLS (Process Details sheet) and build a mapping:
        RLBL_code (str) -> sanitized_task_name (str)

    Columns in Process Details sheet:
        0: Process Id
        1: Process Name
        2: Order
        3: Task Name  (RLBL code for TYPE3, actual name for T2T)
        4: Task Description (human-readable name for TYPE3)
        5: Task Type
    """
    df = pd.read_excel(str(excel_path), sheet_name='Process Details', header=None)

    def clean(val):
        if pd.isna(val): return None
        s = str(val).strip().replace('\n', ' ').replace('\\n', ' ')
        s = re.sub(r'\s+', ' ', s)
        return s if s else None

    def sanitize(name):
        s = re.sub(r'[^A-Za-z0-9_]', '_', name)
        s = re.sub(r'_+', '_', s)
        return s.strip('_')

    mapping = {}
    for _, row in df.iterrows():
        rlbl_code    = clean(row[3])  # e.g. RLBL0275
        task_desc    = clean(row[4])  # e.g. CS - Net Additional Tier 1 Capital
        task_type    = clean(row[5])  # TYPE3 or T2T

        if not rlbl_code or not task_desc:
            continue
        if task_type and 'TYPE3' in task_type.upper():
            if rlbl_code.upper().startswith('RLBL'):
                mapping[rlbl_code.upper()] = sanitize(task_desc)

    return mapping


# ── Extraction ────────────────────────────────────────────────────────────────

def extract_rule_id(content: str) -> str | None:
    m = RE_RULE_CODE.search(content)
    return m.group(1).strip().upper() if m else None


def extract_sql(content: str) -> str | None:
    m = RE_FINAL_QUERY.search(content)
    if not m:
        return None
    sql = m.group(1).strip()
    # SQL runs to end of line — trim at next log timestamp if present
    sql = sql.split('\n')[0].strip()
    return sql


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 4:
        print("Usage: python extract_type3_sql.py <logs_folder> <excel_runchart> <output_folder>")
        sys.exit(1)

    logs_folder   = Path(sys.argv[1])
    excel_path    = Path(sys.argv[2])
    output_folder = Path(sys.argv[3])

    if not logs_folder.exists():
        print(f"ERROR: Logs folder not found: {logs_folder}")
        sys.exit(1)
    if not excel_path.exists():
        print(f"ERROR: Excel file not found: {excel_path}")
        sys.exit(1)

    output_folder.mkdir(parents=True, exist_ok=True)

    # Build rule ID -> task name map from Excel
    print(f"Reading run chart: {excel_path}")
    ruleid_map = build_ruleid_map(excel_path)
    print(f"Found {len(ruleid_map)} TYPE3 task entries in run chart.\n")

    # Find all log files
    log_files = sorted([f for f in logs_folder.iterdir() if f.is_file()])
    print(f"Found {len(log_files)} files in RULE_EXECUTION folder.")

    success = 0
    skipped_no_rule_id = []
    skipped_no_mapping = []
    skipped_no_sql = []
    skipped_duplicate = []
    seen_tasks = {}

    for log_file in log_files:
        content = log_file.read_text(encoding='utf-8', errors='replace')

        rule_id = extract_rule_id(content)
        if not rule_id:
            skipped_no_rule_id.append(log_file.name)
            continue

        task_name = ruleid_map.get(rule_id)
        if not task_name:
            skipped_no_mapping.append((log_file.name, rule_id))
            continue

        sql = extract_sql(content)
        if not sql:
            skipped_no_sql.append((log_file.name, task_name))
            continue

        if task_name in seen_tasks:
            skipped_duplicate.append((log_file.name, task_name, seen_tasks[task_name]))
            continue

        seen_tasks[task_name] = log_file.name
        out_path = output_folder / f"{task_name}.sql"
        out_path.write_text(sql + '\n', encoding='utf-8')
        print(f"  OK  {task_name}.sql")
        success += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"  Extracted:               {success}")
    print(f"  Skipped (no rule ID):    {len(skipped_no_rule_id)}")
    print(f"  Skipped (no mapping):    {len(skipped_no_mapping)}")
    print(f"  Skipped (no SQL):        {len(skipped_no_sql)}")
    print(f"  Skipped (duplicate):     {len(skipped_duplicate)}")

    if skipped_no_rule_id:
        print(f"\nFiles where Rule ID not found:")
        for f in skipped_no_rule_id:
            print(f"  {f}")

    if skipped_no_mapping:
        print(f"\nRule IDs not found in Excel run chart:")
        for f, rid in skipped_no_mapping:
            print(f"  {f}  (rule_id: {rid})")

    if skipped_no_sql:
        print(f"\nFiles where SQL not found:")
        for f, t in skipped_no_sql:
            print(f"  {f}  (task: {t})")

    if skipped_duplicate:
        print(f"\nDuplicate task names (second occurrence skipped):")
        for f, t, first in skipped_duplicate:
            print(f"  {f}  (task: {t}, first: {first})")

    print(f"\nNext step: run wrap_ofsaa_tasks.py on {output_folder}")


if __name__ == '__main__':
    main()