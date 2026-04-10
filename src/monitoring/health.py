"""
RTIE Health Check Module.

Provides health check functions for all external dependencies:
Oracle, Redis, and PostgreSQL. Returns structured status reports
indicating whether the system is healthy or degraded.
"""

from typing import Any, Dict

import psycopg2

from src.tools.schema_tools import SchemaTools
from src.tools.cache_tools import CacheClient
from src.logger import get_logger

logger = get_logger(__name__, concern="app")


class HealthChecker:
    """Checks connectivity to all external dependencies.

    Tests Oracle (SELECT 1 FROM DUAL), Redis (PING), and PostgreSQL
    (SELECT 1) and returns a combined health status.
    """

    def __init__(
        self,
        schema_tools: SchemaTools,
        cache_client: CacheClient,
        postgres_dsn: str,
    ) -> None:
        """Initialize the HealthChecker with connection clients.

        Args:
            schema_tools: Oracle connection tools.
            cache_client: Redis cache client.
            postgres_dsn: PostgreSQL connection string.
        """
        self._schema_tools = schema_tools
        self._cache = cache_client
        self._postgres_dsn = postgres_dsn

    async def check_all(self) -> Dict[str, Any]:
        """Run health checks on all external dependencies.

        Returns:
            Dict with individual service statuses and overall health:
            {
                "oracle": "ok" | "error",
                "redis": "ok" | "error",
                "postgres": "ok" | "error",
                "status": "healthy" | "degraded"
            }
        """
        oracle_status = await self._check_oracle()
        redis_status = await self._check_redis()
        postgres_status = self._check_postgres()

        all_ok = all(
            s == "ok" for s in [oracle_status, redis_status, postgres_status]
        )

        result = {
            "oracle": oracle_status,
            "redis": redis_status,
            "postgres": postgres_status,
            "status": "healthy" if all_ok else "degraded",
        }

        logger.info(f"Health check result: {result}")
        return result

    async def _check_oracle(self) -> str:
        """Check Oracle connectivity with SELECT 1 FROM DUAL.

        Returns:
            'ok' if the query succeeds, 'error' otherwise.
        """
        try:
            ok = await self._schema_tools.check_connection()
            status = "ok" if ok else "error"
        except Exception as exc:
            logger.error(f"Oracle health check failed: {exc}")
            status = "error"
        return status

    async def _check_redis(self) -> str:
        """Check Redis connectivity with PING.

        Returns:
            'ok' if Redis responds, 'error' otherwise.
        """
        try:
            ok = await self._cache.ping()
            status = "ok" if ok else "error"
        except Exception as exc:
            logger.error(f"Redis health check failed: {exc}")
            status = "error"
        return status

    def _check_postgres(self) -> str:
        """Check PostgreSQL connectivity with SELECT 1.

        Returns:
            'ok' if the query succeeds, 'error' otherwise.
        """
        try:
            conn = psycopg2.connect(self._postgres_dsn)
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            conn.close()
            return "ok"
        except Exception as exc:
            logger.error(f"PostgreSQL health check failed: {exc}")
            return "error"
