"""
RTIE SQL Guardian.

Validates all SQL statements before execution to enforce read-only access.
Rejects any DML/DDL operations, enforces bind variable usage, and applies
automatic row fetch limits to prevent unbounded result sets.
"""

import re
from typing import Dict

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


# Tokens and keywords that indicate write operations
FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
    "TRUNCATE", "MERGE",
}


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
