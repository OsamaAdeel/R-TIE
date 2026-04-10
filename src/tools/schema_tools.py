"""
RTIE Schema Tools.

Provides utilities for loading SQL templates from YAML, building
parameterized queries, and managing Oracle connection pooling. All
queries are validated through SQLGuardian before execution.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

import yaml
import oracledb

from src.logger import get_logger
from src.tools.sql_guardian import SQLGuardian
from src.middleware.retry import oracle_retry

logger = get_logger(__name__, concern="oracle")

# Path to the SQL templates file
TEMPLATES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "templates", "sql_templates.yaml"
)


class SchemaTools:
    """Oracle schema introspection and query execution tools.

    Manages an async Oracle connection pool and provides methods for
    executing parameterized, read-only queries loaded from YAML templates.
    All queries pass through SQLGuardian validation before execution.
    """

    def __init__(
        self,
        host: str,
        port: int,
        sid: str,
        user: str,
        password: str,
        pool_min: int = 2,
        pool_max: int = 6,
    ) -> None:
        """Initialize Oracle connection parameters.

        Args:
            host: Oracle database hostname.
            port: Oracle listener port.
            sid: Oracle System Identifier.
            user: Database username.
            password: Database password.
            pool_min: Minimum connections in the pool.
            pool_max: Maximum connections in the pool.
        """
        self._host = host
        self._port = port
        self._sid = sid
        self._user = user
        self._password = password
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._pool: Optional[oracledb.AsyncConnectionPool] = None
        self._guardian = SQLGuardian()
        self._templates: Dict[str, Any] = {}

    async def initialize(self) -> None:
        """Create the async Oracle connection pool and load SQL templates.

        Must be called before executing any queries.
        """
        dsn = oracledb.makedsn(self._host, self._port, sid=self._sid)
        self._pool = oracledb.create_pool_async(
            user=self._user,
            password=self._password,
            dsn=dsn,
            min=self._pool_min,
            max=self._pool_max,
        )
        logger.info(
            f"Oracle async pool created: {self._host}:{self._port}/{self._sid} "
            f"(min={self._pool_min}, max={self._pool_max})"
        )
        self._load_templates()

    def _load_templates(self) -> None:
        """Load SQL templates from the YAML file.

        Raises:
            FileNotFoundError: If the templates file does not exist.
        """
        with open(TEMPLATES_PATH, "r", encoding="utf-8") as f:
            self._templates = yaml.safe_load(f)
        logger.info(f"Loaded {len(self._templates)} SQL templates from {TEMPLATES_PATH}")

    def get_template(self, name: str) -> Dict[str, Any]:
        """Retrieve a SQL template by name.

        Args:
            name: Template key (e.g. 'TMPL_FETCH_SOURCE').

        Returns:
            Dict containing 'sql', 'params', and 'read_only' keys.

        Raises:
            KeyError: If the template name is not found.
        """
        if name not in self._templates:
            raise KeyError(f"SQL template '{name}' not found")
        return self._templates[name]

    @oracle_retry
    async def execute_query(
        self,
        template_name: str,
        params: Dict[str, Any],
        fetch_limit: int = 5000,
    ) -> List[Tuple]:
        """Execute a read-only query from a named template.

        The SQL is validated by SQLGuardian, bind variables are checked,
        and a FETCH FIRST limit is injected before execution.

        Args:
            template_name: Name of the SQL template to execute.
            params: Dictionary of bind variable values.
            fetch_limit: Maximum rows to return. Defaults to 5000.

        Returns:
            List of result tuples from the query.

        Raises:
            GuardianRejectionError: If the SQL fails validation.
            oracledb.DatabaseError: If the query execution fails (retried).
        """
        import time

        template = self.get_template(template_name)
        sql = template["sql"]

        # Validate through SQLGuardian
        self._guardian.validate(sql)
        self._guardian.check_bind_variables(sql, params)
        sql = self._guardian.inject_fetch_limit(sql, limit=fetch_limit)

        start_time = time.time()
        logger.info(f"Executing template: {template_name} with params: {list(params.keys())}")

        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params)
                rows = await cursor.fetchall()

        elapsed_ms = round((time.time() - start_time) * 1000, 2)
        logger.info(
            f"Query {template_name} completed: {len(rows)} rows in {elapsed_ms}ms"
        )
        return rows

    @oracle_retry
    async def execute_raw(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Tuple]:
        """Execute a raw read-only SQL statement.

        The SQL is validated by SQLGuardian before execution.

        Args:
            sql: The raw SQL string.
            params: Optional bind variable dictionary.

        Returns:
            List of result tuples.

        Raises:
            GuardianRejectionError: If the SQL fails validation.
            oracledb.DatabaseError: If the query execution fails (retried).
        """
        import time

        self._guardian.validate(sql)
        if params:
            self._guardian.check_bind_variables(sql, params)

        start_time = time.time()
        logger.info("Executing raw SQL query")

        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params or {})
                rows = await cursor.fetchall()

        elapsed_ms = round((time.time() - start_time) * 1000, 2)
        logger.info(f"Raw query completed: {len(rows)} rows in {elapsed_ms}ms")
        return rows

    async def check_connection(self) -> bool:
        """Test the Oracle connection with a simple query.

        Returns:
            True if SELECT 1 FROM DUAL succeeds, False otherwise.
        """
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 1 FROM DUAL")
                    await cursor.fetchone()
            return True
        except Exception as exc:
            logger.error(f"Oracle health check failed: {exc}")
            return False

    async def close(self) -> None:
        """Close the Oracle connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("Oracle connection pool closed")
