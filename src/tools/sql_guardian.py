"""
RTIE SQL Guardian.

Validates all SQL statements before execution to enforce read-only access.
Rejects any DML/DDL operations, enforces bind variable usage, and applies
automatic row fetch limits to prevent unbounded result sets.
"""

import re
from typing import Dict, Iterable, Mapping, Optional, Set

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import Keyword, DML, DDL

from src.logger import get_logger

logger = get_logger(__name__, concern="oracle")


class GuardianRejectionError(Exception):
    """Raised when SQLGuardian rejects a SQL statement.

    Attributes:
        message: Human-readable description of why the statement was rejected.
    """

    def __init__(self, message: str) -> None:
        """Initialize with a descriptive rejection message.

        Args:
            message: Description of why the SQL was rejected.
        """
        self.message = message
        super().__init__(self.message)


class ColumnResidencyError(GuardianRejectionError):
    """Raised when a SQL references a column against a table it doesn't
    live on.

    Subclass of GuardianRejectionError so existing `except
    GuardianRejectionError` handlers still catch it, but carries the
    offending column/table so callers can produce a specific user message.
    """

    def __init__(self, column: str, table: str, message: Optional[str] = None) -> None:
        self.column = column
        self.table = table
        super().__init__(
            message
            or (
                f"Column '{column}' is not present on table '{table}' "
                "according to the parsed schema catalog."
            )
        )


# Tokens and keywords that indicate write operations
FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
    "TRUNCATE", "MERGE",
}


# Oracle built-ins and SQL keywords that may appear as bare identifiers
# but are never column references. Scoped narrowly on purpose — we only
# need to skip things that look column-shaped to the residency scanner.
_SQL_NON_COLUMN_TOKENS = frozenset({
    # Clause / syntax keywords
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS", "NULL",
    "AS", "ON", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "FULL",
    "CROSS", "GROUP", "BY", "ORDER", "HAVING", "UNION", "ALL", "CASE",
    "WHEN", "THEN", "ELSE", "END", "DISTINCT", "BETWEEN", "LIKE",
    "ASC", "DESC", "FETCH", "FIRST", "NEXT", "ROWS", "ONLY", "OFFSET",
    "WITH",
    # Date / numeric / aggregation built-ins
    "TO_DATE", "TO_CHAR", "TO_NUMBER", "TO_TIMESTAMP", "TRUNC", "ROUND",
    "NVL", "COALESCE", "DECODE", "CAST", "EXTRACT", "SYSDATE",
    "SYSTIMESTAMP", "CURRENT_DATE", "CURRENT_TIMESTAMP", "SUBSTR",
    "INSTR", "LENGTH", "UPPER", "LOWER", "TRIM", "CONCAT",
    "ABS", "GREATEST", "LEAST", "COUNT", "SUM", "AVG", "MIN", "MAX",
    "YEAR", "MONTH", "DAY", "DUAL", "ADD_MONTHS", "LAST_DAY",
    "MONTHS_BETWEEN", "FIRST_VALUE", "LAST_VALUE", "LAG", "LEAD",
    "ROW_NUMBER", "RANK", "DENSE_RANK", "OVER", "PARTITION",
})

# Matches table references in FROM / JOIN clauses:
#   FROM STG_GL_DATA G JOIN STG_PRODUCT_PROCESSOR PP ON ...
# Capturing groups: (1) table name, (2) optional alias (stripped of AS).
_FROM_TABLE_RE = re.compile(
    r"(?:FROM|JOIN)\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)"
    r"(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?",
    re.IGNORECASE,
)

# Qualified column references: ALIAS.COL or SCHEMA.TABLE.COL's last two
# segments. Second group is the column name.
_QUALIFIED_COL_RE = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")

# Bare identifier that looks like a column name (used for unqualified
# column detection). Deliberately narrow: OFSAA conventions use
# N_/V_/F_/D_/FIC_/LD_/SETUP_/B_ prefixes.
_COL_SHAPED_RE = re.compile(
    r"\b((?:N|V|F|D|FIC|LD|SETUP|B)_[A-Za-z0-9_]+)\b"
)


class SQLGuardian:
    """Validates SQL statements to enforce strict read-only access.

    Every query destined for Oracle must pass through this guardian.
    It inspects the SQL AST for forbidden operations, verifies bind
    variable usage, and injects row limits where missing.
    """

    def validate(self, sql: str) -> bool:
        """Validate that a SQL statement contains no DML or DDL operations.

        Parses the SQL using sqlparse and inspects every token. Rejects
        statements containing INSERT, UPDATE, DELETE, DROP, CREATE, ALTER,
        TRUNCATE, or MERGE keywords.

        Args:
            sql: The raw SQL string to validate.

        Returns:
            True if the SQL is safe (read-only).

        Raises:
            GuardianRejectionError: If a forbidden DML/DDL token is found.
        """
        logger.info("Validating SQL statement for read-only compliance")

        parsed_statements = sqlparse.parse(sql)

        for statement in parsed_statements:
            self._check_statement_tokens(statement)

        logger.info("SQL validation passed — statement is read-only")
        return True

    def _check_statement_tokens(self, statement: Statement) -> None:
        """Recursively inspect all tokens in a parsed SQL statement.

        Args:
            statement: A sqlparse Statement object.

        Raises:
            GuardianRejectionError: If any forbidden keyword is detected.
        """
        for token in statement.flatten():
            if token.ttype in (DML, DDL, Keyword.DML, Keyword.DDL):
                word = token.normalized.upper()
                if word in FORBIDDEN_KEYWORDS:
                    msg = (
                        f"SQL Guardian REJECTED: forbidden keyword '{word}' detected. "
                        f"RTIE is read-only — DML/DDL operations are not permitted."
                    )
                    logger.error(msg)
                    raise GuardianRejectionError(msg)

            # Also check normalized value for keywords that may not have DML/DDL ttype
            if token.normalized and token.normalized.upper() in FORBIDDEN_KEYWORDS:
                word = token.normalized.upper()
                msg = (
                    f"SQL Guardian REJECTED: forbidden keyword '{word}' detected. "
                    f"RTIE is read-only — DML/DDL operations are not permitted."
                )
                logger.error(msg)
                raise GuardianRejectionError(msg)

    def inject_fetch_limit(self, sql: str, limit: int = 5000) -> str:
        """Append a FETCH FIRST N ROWS ONLY clause if not already present.

        Prevents unbounded result sets from overwhelming memory or network.

        Args:
            sql: The SQL string to modify.
            limit: Maximum number of rows to fetch. Defaults to 5000.

        Returns:
            The SQL string with a FETCH FIRST clause appended if it was missing.
        """
        sql_upper = sql.upper().strip()
        if "FETCH FIRST" in sql_upper or "FETCH NEXT" in sql_upper:
            logger.debug("SQL already contains FETCH clause — skipping injection")
            return sql

        stripped = sql.rstrip().rstrip(";")
        result = f"{stripped}\nFETCH FIRST {limit} ROWS ONLY"
        logger.info(f"Injected FETCH FIRST {limit} ROWS ONLY into SQL statement")
        return result

    def check_bind_variables(self, sql: str, params: Dict[str, object]) -> bool:
        """Verify that the SQL uses bind variable syntax, not string interpolation.

        Scans for common string interpolation patterns (f-string placeholders,
        %-formatting, .format() calls) and ensures all parameter names appear
        as Oracle bind variables (:param_name) in the SQL.

        Args:
            sql: The SQL string to check.
            params: Dictionary of parameter names and their values.

        Returns:
            True if all parameters use bind variable syntax.

        Raises:
            GuardianRejectionError: If string interpolation is detected or
                a parameter is missing its bind variable.
        """
        logger.info("Checking SQL for proper bind variable usage")

        # Detect string interpolation patterns
        interpolation_patterns = [
            (r"\{[^}]*\}", "f-string / .format() placeholder"),
            (r"%[sdifr]", "%-style format specifier"),
            (r"%\([^)]+\)[sdifr]", "%(name)s format specifier"),
        ]

        for pattern, description in interpolation_patterns:
            if re.search(pattern, sql):
                msg = (
                    f"SQL Guardian REJECTED: possible string interpolation detected "
                    f"({description}). Use Oracle bind variables (:param) instead."
                )
                logger.error(msg)
                raise GuardianRejectionError(msg)

        # Verify each expected param exists as a bind variable
        for param_name in params:
            bind_pattern = rf":{param_name}(?!\w)"
            if not re.search(bind_pattern, sql):
                msg = (
                    f"SQL Guardian REJECTED: parameter '{param_name}' not found as "
                    f"bind variable (:{param_name}) in SQL statement."
                )
                logger.error(msg)
                raise GuardianRejectionError(msg)

        logger.info("Bind variable check passed — all parameters use :param syntax")
        return True

    def validate_column_residency(
        self,
        sql: str,
        tables_to_columns: Mapping[str, Iterable[str]],
    ) -> bool:
        """Reject SQL that uses a column against a table it doesn't live on.

        Second line of defense against LLM column hallucination. Intended
        to run AFTER `validate` + `check_bind_variables`.

        Strategy:
          1. Extract FROM / JOIN targets and their aliases from the SQL.
          2. For each qualified column (`alias.col`), check the column is
             on the aliased table's known column set.
          3. For each bare column (unqualified, matching OFSAA naming
             conventions), check it appears in the union of all FROM
             tables' column sets.

        Tables not present in `tables_to_columns` are skipped (unknown —
        we can't prove anything about them without a catalog, so we
        assume they're valid). If the SQL has NO known tables at all,
        the check is skipped — no false positives on exotic queries.

        Args:
            sql: The SQL string.
            tables_to_columns: `{TABLE_NAME: {COL, COL, ...}}` catalog.

        Returns:
            True when every referenced column resides on a FROM table.

        Raises:
            ColumnResidencyError: Column exists in the catalog but not on
                the table it's being used with (the classic hallucination
                — e.g. `V_ACCOUNT_NUMBER` on `STG_GL_DATA`).
        """
        if not tables_to_columns:
            logger.debug(
                "validate_column_residency skipped — empty catalog "
                "(nothing to compare against)"
            )
            return True

        canonical: Dict[str, Set[str]] = {
            str(t).upper(): {str(c).upper() for c in cols}
            for t, cols in tables_to_columns.items()
        }

        sql_text = _strip_string_literals(sql)

        alias_to_table: Dict[str, str] = {}
        from_tables: Set[str] = set()
        for match in _FROM_TABLE_RE.finditer(sql_text):
            raw_table = match.group(1)
            alias = match.group(2)
            # Drop schema prefix if present: OFSMDM.STG_GL_DATA -> STG_GL_DATA
            table = raw_table.split(".")[-1].upper()
            from_tables.add(table)
            alias_to_table[table] = table  # table name can be used as its own qualifier
            if alias:
                alias_up = alias.upper()
                if alias_up not in _SQL_NON_COLUMN_TOKENS:
                    alias_to_table[alias_up] = table

        known_from_tables = {t for t in from_tables if t in canonical}
        if not known_from_tables:
            logger.debug(
                "validate_column_residency skipped — no FROM tables have "
                "a catalog entry (from=%s)", sorted(from_tables),
            )
            return True

        union_cols: Set[str] = set()
        for t in known_from_tables:
            union_cols.update(canonical[t])

        # 1. Qualified column references — check against the qualifier's
        #    table, when we know it. Collect column names we've already
        #    validated so the unqualified pass can skip them.
        validated_quals: Set[str] = set()
        for match in _QUALIFIED_COL_RE.finditer(sql_text):
            qualifier = match.group(1).upper()
            col = match.group(2).upper()
            if col in _SQL_NON_COLUMN_TOKENS or col.isdigit():
                continue
            table = alias_to_table.get(qualifier)
            if table is None:
                # Qualifier isn't a known FROM table/alias — e.g. a CTE
                # or subquery alias. Skip: we can't prove anything.
                continue
            if table not in canonical:
                # Qualifier is a real table but we don't have its columns.
                continue
            if col not in canonical[table]:
                # Genuine residency violation. But before raising, see if
                # the column exists on ANY known FROM table — if so, the
                # LLM just qualified it wrong, still a bug to report.
                elsewhere = next(
                    (t for t in known_from_tables if col in canonical[t]),
                    None,
                )
                msg = (
                    f"Column '{col}' is not present on table '{table}' "
                    "according to the parsed schema catalog"
                )
                if elsewhere:
                    msg += f" (it appears on '{elsewhere}' instead)"
                msg += "."
                logger.error(
                    "Column residency violation | column=%s asserted_table=%s "
                    "actual_table=%s",
                    col, table, elsewhere or "unknown",
                )
                raise ColumnResidencyError(column=col, table=table, message=msg)
            validated_quals.add(col)

        # 2. Unqualified column-shaped identifiers — must appear on at
        #    least one FROM table. Skip tokens we've already validated as
        #    qualified references and skip any bare token that happens to
        #    match a FROM table name (e.g. `STG_GL_DATA` in a subquery).
        from_table_names = {t for t in from_tables}
        for match in _COL_SHAPED_RE.finditer(sql_text):
            col = match.group(1).upper()
            if col in _SQL_NON_COLUMN_TOKENS:
                continue
            if col in from_table_names:
                continue
            if col in validated_quals:
                continue
            # If this position was already captured as `alias.col` above,
            # the qualified pass handled it — skip.
            start = match.start(1)
            if start > 0 and sql_text[start - 1] == ".":
                continue
            if col not in union_cols:
                logger.error(
                    "Column residency violation | column=%s not on any "
                    "FROM table %s",
                    col, sorted(known_from_tables),
                )
                raise ColumnResidencyError(
                    column=col,
                    table=", ".join(sorted(known_from_tables)),
                    message=(
                        f"Column '{col}' is not present on any FROM table "
                        f"({', '.join(sorted(known_from_tables))}) "
                        "according to the parsed schema catalog."
                    ),
                )

        logger.info(
            "Column residency check passed — every column resolves to a "
            "FROM table"
        )
        return True


def _strip_string_literals(sql: str) -> str:
    """Replace single-quoted string contents with empty strings so regex
    scans don't pick up column-shaped tokens that appear inside literals
    (e.g. `WHERE V_CODE = 'V_ACCOUNT_NUMBER'`).
    """
    return re.sub(r"'(?:[^']|'')*'", "''", sql)
