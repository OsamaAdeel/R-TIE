"""
RTIE Cache Tools.

Provides async Redis client management and utility functions for
reading, writing, and managing cached logic objects. Handles graceful
degradation when Redis is unavailable.
"""

import json
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis

from src.logger import get_logger

logger = get_logger(__name__, concern="cache")


class CacheClient:
    """Async Redis client wrapper with graceful degradation.

    All cache operations are wrapped in try/except blocks to ensure
    Redis unavailability never causes the system to fail. On connection
    errors, operations log a WARNING and return None or empty results.
    """

    def __init__(self, host: str, port: int, key_prefix: str = "rtie") -> None:
        """Initialize the Redis cache client.

        Args:
            host: Redis server hostname.
            port: Redis server port.
            key_prefix: Prefix for all cache keys. Defaults to 'rtie'.
        """
        self._host = host
        self._port = port
        self._key_prefix = key_prefix
        self._client: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        """Establish connection to Redis.

        Logs a warning if connection fails — does not raise.
        """
        try:
            self._client = aioredis.Redis(
                host=self._host,
                port=self._port,
                decode_responses=True,
            )
            await self._client.ping()
            logger.info(f"Connected to Redis at {self._host}:{self._port}")
        except Exception as exc:
            logger.warning(f"Redis connection failed: {exc}. Operating without cache.")
            self._client = None

    async def close(self) -> None:
        """Close the Redis connection if open."""
        if self._client:
            await self._client.close()
            logger.info("Redis connection closed")

    def _key(self, *parts: str) -> str:
        """Build a namespaced cache key.

        Args:
            *parts: Key segments to join with ':'.

        Returns:
            Full cache key prefixed with the configured key_prefix.
        """
        return ":".join([self._key_prefix, *parts])

    async def get_json(self, *key_parts: str) -> Optional[Dict[str, Any]]:
        """Retrieve a JSON-serialized value from Redis.

        Args:
            *key_parts: Key segments (joined with ':' after prefix).

        Returns:
            Parsed dict if found, None on cache miss or Redis error.
        """
        if not self._client:
            logger.warning("Redis unavailable — cache miss (no client)")
            return None
        try:
            key = self._key(*key_parts)
            raw = await self._client.get(key)
            if raw is None:
                logger.info(f"Cache MISS for key: {key}")
                return None
            logger.info(f"Cache HIT for key: {key}")
            return json.loads(raw)
        except Exception as exc:
            logger.warning(f"Redis GET failed for key {key_parts}: {exc}")
            return None

    async def set_json(
        self,
        value: Dict[str, Any],
        *key_parts: str,
        ttl: Optional[int] = None,
    ) -> bool:
        """Store a JSON-serialized value in Redis.

        Args:
            value: Dictionary to serialize and store.
            *key_parts: Key segments (joined with ':' after prefix).
            ttl: Time-to-live in seconds. None means no expiration.

        Returns:
            True if stored successfully, False on error.
        """
        if not self._client:
            logger.warning("Redis unavailable — skipping cache write")
            return False
        try:
            key = self._key(*key_parts)
            payload = json.dumps(value, default=str)
            if ttl:
                await self._client.setex(key, ttl, payload)
            else:
                await self._client.set(key, payload)
            logger.info(f"Cache SET for key: {key}")
            return True
        except Exception as exc:
            logger.warning(f"Redis SET failed for key {key_parts}: {exc}")
            return False

    async def delete_key(self, *key_parts: str) -> bool:
        """Delete a key from Redis.

        Args:
            *key_parts: Key segments identifying the key to delete.

        Returns:
            True if deleted, False on error or Redis unavailability.
        """
        if not self._client:
            logger.warning("Redis unavailable — cannot delete key")
            return False
        try:
            key = self._key(*key_parts)
            await self._client.delete(key)
            logger.info(f"Cache DELETE for key: {key}")
            return True
        except Exception as exc:
            logger.warning(f"Redis DELETE failed for key {key_parts}: {exc}")
            return False

    async def list_keys(self, pattern: str) -> List[str]:
        """List all keys matching a pattern.

        Args:
            pattern: Redis SCAN pattern (e.g. 'logic:OFSMDM:*').

        Returns:
            List of matching key strings, or empty list on error.
        """
        if not self._client:
            logger.warning("Redis unavailable — cannot list keys")
            return []
        try:
            full_pattern = self._key(pattern)
            keys: List[str] = []
            async for key in self._client.scan_iter(match=full_pattern):
                keys.append(key)
            logger.info(f"Cache LIST found {len(keys)} keys matching: {full_pattern}")
            return keys
        except Exception as exc:
            logger.warning(f"Redis SCAN failed for pattern {pattern}: {exc}")
            return []

    async def ping(self) -> bool:
        """Check if Redis is reachable.

        Returns:
            True if Redis responds to PING, False otherwise.
        """
        if not self._client:
            return False
        try:
            return await self._client.ping()
        except Exception:
            return False
