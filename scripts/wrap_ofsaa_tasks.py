"""
RTIE — OFSAA task SQL wrapping script
=====================================

Wraps raw MERGE / INSERT / UPDATE statements (extracted from OFSAA
execution logs or metadata) inside CREATE OR REPLACE FUNCTION blocks
so RTIE's parser can identify them as functions.

What it does:
  - Scans a target directory for .sql files
  - Skips files that already start with CREATE OR REPLACE FUNCTION
  - Wraps everything else in the standard CSTM DT signature
  - Writes the wrapped version back to the same file
  - Creates a .bak backup of each file before modifying

What it does NOT do:
  - Does not change the SQL logic inside the wrapper
  - Does not infer task type (that's declared in manifest.yaml)
  - Does not delete or rename files
  - Does not touch files that are already wrapped

Usage:
    python wrap_ofsaa_tasks.py <folder> [--schema OFSERM] [--dry-run]

Example:
    cd C:/path/to/RTIE
    python wrap_ofsaa_tasks.py db/modules/ABL_CAR_CSTM_V4/functions
    
To preview without writing:
    python wrap_ofsaa_tasks.py db/modules/ABL_CAR_CSTM_V4/functions --dry-run

To undo:
    For each .sql file in the folder, if a .bak exists, copy .bak back over .sql
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path


WRAPPER_HEADER_TEMPLATE = """-- =====================================================================
-- Task: {function_name}
-- Schema: {schema}
-- Wrapped: {wrap_date}
-- Source: OFSAA execution log / metadata extraction
-- =====================================================================
CREATE OR REPLACE FUNCTION {schema}.{function_name}(
    P_V_BATCH_ID         VARCHAR2,
    P_V_MIS_DATE         VARCHAR2,
    P_V_RUN_ID           VARCHAR2,
    P_V_PROCESS_ID       VARCHAR2,
    P_V_RUN_EXECUTION_ID VARCHAR2,
    P_N_RUN_SKEY         VARCHAR2,
    P_V_TASK_ID          VARCHAR2
) RETURN VARCHAR2 AUTHID CURRENT_USER AS

    ld_mis_date   DATE          := TO_DATE(P_V_MIS_DATE, 'YYYYMMDD');
    ln_mis_date   NUMBER        := TO_NUMBER(P_V_MIS_DATE);
    ln_run_skey   NUMBER(5)     := TO_NUMBER(SUBSTR(P_N_RUN_SKEY, 8, LENGTH(P_N_RUN_SKEY)));
    lv_run_id     VARCHAR2(64)  := SUBSTR(P_V_RUN_ID, 8, LENGTH(P_V_RUN_ID));

BEGIN

"""

WRAPPER_FOOTER = """

    COMMIT;
    RETURN 'OK';

EXCEPTION
    WHEN OTHERS THEN
        ROLLBACK;
        RETURN 'FAIL: ' || SQLERRM;
END;
/
"""


def is_already_wrapped(content: str) -> bool:
    """Return True if the file already has a CREATE OR REPLACE FUNCTION header."""
    # Check first ~500 chars for the wrapper; ignore leading comments/whitespace
    head = content[:500].upper()
    return "CREATE OR REPLACE FUNCTION" in head


def normalize_body(raw: str) -> str:
    """
    Prepare the raw SQL body for wrapping:
      - strip leading/trailing whitespace
      - ensure it ends with a semicolon (Oracle requires it inside BEGIN/END)
      - remove any trailing standalone '/' that might have been copied from
        a sqlplus session (we add our own trailing slash after END;)
    """
    body = raw.strip()

    # Remove trailing lone '/' if present (common in sqlplus dumps)
    if body.endswith("/"):
        # Only strip if '/' is on its own line, not part of division syntax
        lines = body.splitlines()
        if lines and lines[-1].strip() == "/":
            body = "\n".join(lines[:-1]).rstrip()

    # Ensure body ends with exactly one semicolon
    if not body.endswith(";"):
        body += ";"

    return body


def wrap_file(sql_file: Path, schema: str, dry_run: bool = False) -> str:
    """
    Wrap a single .sql file. Returns a status string describing what happened.
    """
    content = sql_file.read_text(encoding="utf-8")

    if not content.strip():
        return f"SKIPPED (empty file): {sql_file.name}"

    if is_already_wrapped(content):
        return f"SKIPPED (already wrapped): {sql_file.name}"

    function_name = sql_file.stem  # filename without .sql
    body = normalize_body(content)

    # Indent the body by 4 spaces so it reads cleanly inside BEGIN/END
    indented_body = "\n".join("    " + line if line else line
                              for line in body.splitlines())

    wrapped = (
        WRAPPER_HEADER_TEMPLATE.format(
            function_name=function_name,
            schema=schema,
            wrap_date=datetime.now().strftime("%Y-%m-%d"),
        )
        + indented_body
        + WRAPPER_FOOTER
    )

    if dry_run:
        return f"WOULD WRAP: {sql_file.name} (function_name={function_name}, schema={schema})"

    # Create a .bak backup before writing
    backup_path = sql_file.with_suffix(".sql.bak")
    if not backup_path.exists():
        shutil.copy2(sql_file, backup_path)

    sql_file.write_text(wrapped, encoding="utf-8")
    return f"WRAPPED: {sql_file.name} (function_name={function_name}, schema={schema})"


def main():
    parser = argparse.ArgumentParser(
        description="Wrap OFSAA-extracted SQL files as PL/SQL functions."
    )
    parser.add_argument(
        "folder",
        help="Path to the functions/ folder containing .sql files to wrap.",
    )
    parser.add_argument(
        "--schema",
        default="OFSERM",
        help="Oracle schema prefix for CREATE OR REPLACE FUNCTION. Default: OFSERM",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be wrapped without modifying any files.",
    )
    args = parser.parse_args()

    folder = Path(args.folder).resolve()

    if not folder.exists():
        print(f"ERROR: folder does not exist: {folder}")
        sys.exit(1)

    if not folder.is_dir():
        print(f"ERROR: not a directory: {folder}")
        sys.exit(1)

    sql_files = sorted(folder.glob("*.sql"))

    if not sql_files:
        print(f"No .sql files found in {folder}")
        sys.exit(0)

    print(f"Scanning {len(sql_files)} .sql files in {folder}")
    print(f"Schema: {args.schema}")
    print(f"Dry run: {args.dry_run}")
    print("-" * 70)

    wrapped_count = 0
    skipped_count = 0
    error_count = 0

    for sql_file in sql_files:
        try:
            status = wrap_file(sql_file, args.schema, args.dry_run)
            print(status)
            if status.startswith("WRAPPED") or status.startswith("WOULD WRAP"):
                wrapped_count += 1
            else:
                skipped_count += 1
        except Exception as e:
            print(f"ERROR processing {sql_file.name}: {e}")
            error_count += 1

    print("-" * 70)
    print(
        f"Summary: "
        f"{wrapped_count} wrapped, "
        f"{skipped_count} skipped, "
        f"{error_count} errored"
    )

    if args.dry_run:
        print()
        print("This was a dry run. No files were modified.")
        print("Re-run without --dry-run to apply changes.")
    elif wrapped_count > 0:
        print()
        print(f"Backups saved as <filename>.sql.bak alongside each wrapped file.")
        print(f"To undo: delete the .sql file and rename the .bak back to .sql.")


if __name__ == "__main__":
    main()