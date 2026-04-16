"""
RTIE Vector Store Tools.

Provides async Redis vector store operations using RediSearch for
semantic search over PL/SQL function descriptions. Handles index
creation, document storage with embeddings, and KNN similarity search.
"""

import hashlib
import struct
from datetime import datetime
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis
from redis.commands.search.field import TextField, TagField, VectorField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from src.logger import get_logger

logger = get_logger(__name__, concern="cache")


class VectorStore:
    """Async Redis vector store client for semantic search.

    Manages a RediSearch index over PL/SQL function description embeddings.
    All operations degrade gracefully when Redis is unavailable.
    """

    EMBEDDING_DIM = 1536
    INDEX_NAME = "idx:rtie_vectors"
    KEY_PREFIX = "rtie:vec:"

    def __init__(self, host: str, port: int) -> None:
        """Initialize the vector store client.

        Args:
            host: Redis server hostname.
            port: Redis server port.
        """
        self._host = host
        self._port = port
        self._client: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        """Establish connection to Redis.

        Logs a warning if connection fails — does not raise.
        """
        try:
            self._client = aioredis.Redis(
                host=self._host,
                port=self._port,
                decode_responses=False,
            )
            await self._client.ping()
            logger.info(f"VectorStore connected to Redis at {self._host}:{self._port}")
        except Exception as exc:
            logger.warning(f"VectorStore Redis connection failed: {exc}")
            self._client = None

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._client:
            await self._client.close()
            logger.info("VectorStore connection closed")

    async def ensure_index(self) -> bool:
        """Create the RediSearch vector index if it does not exist.

        Returns:
            True if the index exists or was created, False on error.
        """
        if not self._client:
            logger.warning("VectorStore unavailable — cannot create index")
            return False

        try:
            await self._client.ft(self.INDEX_NAME).info()
            logger.info(f"Index {self.INDEX_NAME} already exists")
            return True
        except Exception:
            pass

        try:
            schema = (
                TextField("function_name"),
                TagField("module"),
                TextField("description"),
                TextField("tables_read"),
                TextField("tables_written"),
                TextField("key_columns"),
                TagField("status"),
                TextField("generated_at"),
                TextField("description_hash"),
                TextField("source_hash"),
                VectorField(
                    "embedding",
                    "FLAT",
                    {
                        "TYPE": "FLOAT32",
                        "DIM": self.EMBEDDING_DIM,
                        "DISTANCE_METRIC": "COSINE",
                    },
                ),
            )
            definition = IndexDefinition(
                prefix=[self.KEY_PREFIX], index_type=IndexType.HASH
            )
            await self._client.ft(self.INDEX_NAME).create_index(
                schema, definition=definition
            )
            logger.info(f"Created RediSearch index: {self.INDEX_NAME}")
            return True
        except Exception as exc:
            logger.error(f"Failed to create vector index: {exc}")
            return False

    async def upsert_function(
        self,
        module: str,
        function_name: str,
        description: str,
        embedding: List[float],
        tables_read: List[str],
        tables_written: List[str],
        key_columns: List[str],
        source_hash: str,
        status: str = "approved",
    ) -> bool:
        """Store or update a function's description and embedding.

        Args:
            module: Module/batch name.
            function_name: PL/SQL function name.
            description: LLM-generated rich description.
            embedding: Float vector from embedding model.
            tables_read: List of tables the function reads.
            tables_written: List of tables the function writes.
            key_columns: List of key columns referenced.
            source_hash: SHA256 of the source code.
            status: 'approved' or 'pending'. Defaults to 'approved'.

        Returns:
            True if stored successfully, False on error.
        """
        if not self._client:
            logger.warning("VectorStore unavailable — skipping upsert")
            return False

        try:
            key = self._doc_key(module, function_name)
            description_hash = hashlib.sha256(description.encode()).hexdigest()[:16]
            mapping = {
                b"function_name": function_name.encode(),
                b"module": module.encode(),
                b"description": description.encode(),
                b"tables_read": ",".join(tables_read).encode(),
                b"tables_written": ",".join(tables_written).encode(),
                b"key_columns": ",".join(key_columns).encode(),
                b"status": status.encode(),
                b"generated_at": datetime.utcnow().isoformat().encode(),
                b"description_hash": description_hash.encode(),
                b"source_hash": source_hash.encode(),
                b"embedding": self._float_list_to_bytes(embedding),
            }
            await self._client.hset(key, mapping=mapping)
            logger.info(f"Indexed function: {module}:{function_name}")
            return True
        except Exception as exc:
            logger.error(f"Failed to upsert {module}:{function_name}: {exc}")
            return False

    async def search(
        self,
        query_embedding: List[float],
        top_k: int = 3,
        module_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """KNN vector similarity search for functions.

        Args:
            query_embedding: Query vector from embedding model.
            top_k: Number of results to return. Defaults to 3.
            module_filter: Optional module name to scope search.

        Returns:
            List of result dicts with function_name, module, description,
            tables_read, tables_written, key_columns, and score.
        """
        if not self._client:
            logger.warning("VectorStore unavailable — returning empty results")
            return []

        try:
            blob = self._float_list_to_bytes(query_embedding)

            filter_clause = f"@module:{{{module_filter}}}" if module_filter else "*"
            q = (
                Query(f"({filter_clause})=>[KNN {top_k} @embedding $vec AS score]")
                .sort_by("score")
                .return_fields(
                    "function_name", "module", "description",
                    "tables_read", "tables_written", "key_columns", "score",
                )
                .dialect(2)
            )
            results = await self._client.ft(self.INDEX_NAME).search(
                q, query_params={"vec": blob}
            )

            hits = []
            for doc in results.docs:
                hits.append({
                    "function_name": self._decode(doc.function_name),
                    "module": self._decode(doc.module),
                    "description": self._decode(doc.description),
                    "tables_read": self._decode(doc.tables_read),
                    "tables_written": self._decode(doc.tables_written),
                    "key_columns": self._decode(doc.key_columns),
                    "score": float(doc.score),
                })
            logger.info(f"Vector search returned {len(hits)} results")
            return hits
        except Exception as exc:
            logger.error(f"Vector search failed: {exc}")
            return []

    async def get_function_doc(
        self, module: str, function_name: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a function's indexed document.

        Args:
            module: Module/batch name.
            function_name: PL/SQL function name.

        Returns:
            Dict of stored fields, or None if not found.
        """
        if not self._client:
            return None
        try:
            key = self._doc_key(module, function_name)
            data = await self._client.hgetall(key)
            if not data:
                return None
            return {k.decode(): v.decode() for k, v in data.items() if k != b"embedding"}
        except Exception as exc:
            logger.warning(f"Failed to get doc {module}:{function_name}: {exc}")
            return None

    async def delete_function(self, module: str, function_name: str) -> bool:
        """Delete a function from the vector index.

        Args:
            module: Module/batch name.
            function_name: PL/SQL function name.

        Returns:
            True if deleted, False on error.
        """
        if not self._client:
            return False
        try:
            key = self._doc_key(module, function_name)
            await self._client.delete(key)
            logger.info(f"Deleted vector doc: {module}:{function_name}")
            return True
        except Exception as exc:
            logger.warning(f"Failed to delete {module}:{function_name}: {exc}")
            return False

    async def list_indexed_functions(
        self, module: Optional[str] = None
    ) -> List[str]:
        """List all indexed function names.

        Args:
            module: Optional module filter.

        Returns:
            List of function names.
        """
        if not self._client:
            return []
        try:
            pattern = f"{self.KEY_PREFIX}{module}:*" if module else f"{self.KEY_PREFIX}*"
            keys = []
            async for key in self._client.scan_iter(match=pattern.encode()):
                name = key.decode().split(":")[-1]
                keys.append(name)
            return keys
        except Exception as exc:
            logger.warning(f"Failed to list indexed functions: {exc}")
            return []

    async def get_index_stats(self) -> Dict[str, Any]:
        """Get vector index statistics.

        Returns:
            Dict with index info or error status.
        """
        if not self._client:
            return {"status": "unavailable"}
        try:
            info = await self._client.ft(self.INDEX_NAME).info()
            return {
                "status": "ok",
                "index_name": self.INDEX_NAME,
                "num_docs": info.get("num_docs", info.get(b"num_docs", 0)),
                "num_records": info.get("num_records", info.get(b"num_records", 0)),
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _doc_key(self, module: str, function_name: str) -> str:
        """Build a Redis key for a function document.

        Args:
            module: Module/batch name.
            function_name: PL/SQL function name.

        Returns:
            Redis key string.
        """
        return f"{self.KEY_PREFIX}{module}:{function_name}"

    @staticmethod
    def _float_list_to_bytes(floats: List[float]) -> bytes:
        """Convert a list of floats to raw bytes for Redis VECTOR field.

        Args:
            floats: List of float values.

        Returns:
            Packed bytes in float32 format.
        """
        return struct.pack(f"{len(floats)}f", *floats)

    @staticmethod
    def _decode(value: Any) -> str:
        """Safely decode a Redis value to string.

        Args:
            value: Bytes or string value.

        Returns:
            Decoded string.
        """
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value) if value is not None else ""
