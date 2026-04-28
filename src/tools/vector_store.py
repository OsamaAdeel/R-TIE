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

    Phase 3 layout:
      - Doc keys: ``rtie:vec:<schema>:<fn>`` (was ``rtie:vec:<module>:<fn>``).
        The schema segment makes function names unambiguous across the
        OFSMDM/OFSERM corpora.
      - Schema is a ``TAG`` field, queryable as ``@schema:{OFSERM}`` and
        combinable with the KNN clause (e.g. ``(@schema:{OFSERM})=>[KNN ...]``).
      - Module remains a ``TAG`` for module-scoped legacy queries.

    The prefix and TAG addition are not backward-compatible with the
    Phase-2 index — :meth:`ensure_index` detects an old-shape index at
    startup and drops + recreates it. The indexer reindexes from scratch
    after that, taking the one-time embedding-call cost.
    """

    EMBEDDING_DIM = 1536
    INDEX_NAME = "idx:rtie_vectors"
    # Phase 3: prefix changed to include schema. Anything keyed under
    # the old `rtie:vec:<module>:<fn>` shape is purged on FLUSHDB +
    # restart; the index is dropped and recreated on first
    # ensure_index() call after the upgrade.
    KEY_PREFIX = "rtie:vec:"
    SCHEMA_FIELD = "schema"

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

        Phase 3: detect an existing index that pre-dates the schema TAG
        addition (i.e. its FT.INFO does NOT report a ``schema`` attribute)
        and drop + recreate it so the new field can be added. RediSearch
        does not support adding fields to an existing index. The drop
        also clears any stale documents under the old
        ``rtie:vec:<module>:<fn>`` key shape; the indexer rebuilds them
        on the next startup pass.

        Returns:
            True if the index exists with the Phase-3 shape (or was
            created), False on error.
        """
        if not self._client:
            logger.warning("VectorStore unavailable — cannot create index")
            return False

        existing_attrs = await self._index_attribute_names()
        if existing_attrs is not None:
            if self.SCHEMA_FIELD in existing_attrs:
                logger.info(
                    f"Index {self.INDEX_NAME} already exists with Phase-3 "
                    f"schema field"
                )
                return True
            # Pre-Phase-3 index — drop it and any documents indexed under
            # the old key shape, then recreate.
            logger.info(
                "Index %s missing '%s' attribute (pre-Phase-3 shape); "
                "dropping and recreating",
                self.INDEX_NAME, self.SCHEMA_FIELD,
            )
            try:
                # delete_documents=True purges the underlying HASH keys so
                # the next indexer pass writes fresh under the new prefix.
                await self._client.ft(self.INDEX_NAME).dropindex(
                    delete_documents=True
                )
            except Exception as exc:
                logger.warning(
                    "Drop of pre-Phase-3 index failed (continuing anyway): %s",
                    exc,
                )

        try:
            schema = (
                TextField("function_name"),
                TagField(self.SCHEMA_FIELD),
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

    async def _index_attribute_names(self) -> Optional[set[str]]:
        """Return the set of attribute names declared on the existing index.

        Returns ``None`` when no index exists. RediSearch's INFO response
        nests attributes under the ``attributes`` key as a list of
        ``[identifier, name, ...]`` pairs (binary on this client) — pull
        the human-readable names out so the caller can probe for the
        ``schema`` field without re-running create_index.
        """
        try:
            info = await self._client.ft(self.INDEX_NAME).info()
        except Exception:
            return None
        attrs_raw = info.get("attributes") or info.get(b"attributes") or []
        names: set[str] = set()
        for attr in attrs_raw:
            # attr is a flat list like
            # [b'identifier', b'schema', b'attribute', b'schema',
            #  b'type', b'TAG', ...]. Walk it as key/value pairs and pull
            # the value of either 'identifier' or 'attribute'.
            if isinstance(attr, (list, tuple)):
                pairs = list(attr)
                for i in range(0, len(pairs) - 1, 2):
                    key = pairs[i]
                    value = pairs[i + 1]
                    if isinstance(key, (bytes, bytearray)):
                        key = key.decode("utf-8", errors="replace")
                    if isinstance(value, (bytes, bytearray)):
                        value = value.decode("utf-8", errors="replace")
                    if key in ("identifier", "attribute"):
                        names.add(str(value))
            elif isinstance(attr, dict):
                for k in ("identifier", "attribute"):
                    val = attr.get(k) or attr.get(k.encode())
                    if isinstance(val, (bytes, bytearray)):
                        val = val.decode("utf-8", errors="replace")
                    if val:
                        names.add(str(val))
        return names

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
        schema: Optional[str] = None,
    ) -> bool:
        """Store or update a function's description and embedding.

        Args:
            module: Module/batch name (legacy TAG, retained for module-
                scoped searches).
            function_name: PL/SQL function name.
            description: LLM-generated rich description.
            embedding: Float vector from embedding model.
            tables_read: List of tables the function reads.
            tables_written: List of tables the function writes.
            key_columns: List of key columns referenced.
            source_hash: SHA256 of the source code.
            status: 'approved' or 'pending'. Defaults to 'approved'.
            schema: Phase 3 — Oracle owner the function belongs to (e.g.
                ``OFSMDM`` or ``OFSERM``). Stored as the new ``schema``
                TAG and used to namespace the doc key under
                ``rtie:vec:<schema>:<fn>``. ``None`` is accepted for
                back-compat but logged as a warning so the gap is visible.

        Returns:
            True if stored successfully, False on error.
        """
        if not self._client:
            logger.warning("VectorStore unavailable — skipping upsert")
            return False

        if not schema:
            logger.warning(
                "upsert_function: schema not provided for %s:%s; the doc "
                "will land under the empty-schema key. Phase 3 callers "
                "must pass schema.",
                module, function_name,
            )
            schema_str = ""
        else:
            schema_str = schema

        try:
            key = self._doc_key(schema_str, function_name)
            description_hash = hashlib.sha256(description.encode()).hexdigest()[:16]
            mapping = {
                b"function_name": function_name.encode(),
                self.SCHEMA_FIELD.encode(): schema_str.encode(),
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
            logger.info(
                "Indexed function: %s:%s:%s",
                schema_str or "?", module, function_name,
            )
            return True
        except Exception as exc:
            logger.error(f"Failed to upsert {module}:{function_name}: {exc}")
            return False

    async def search(
        self,
        query_embedding: List[float],
        top_k: int = 3,
        module_filter: Optional[str] = None,
        schema_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """KNN vector similarity search for functions.

        Args:
            query_embedding: Query vector from embedding model.
            top_k: Number of results to return. Defaults to 3.
            module_filter: Optional module name to scope search.
            schema_filter: Phase 3 — optional Oracle owner to scope search
                (e.g. ``OFSERM``). Combined with ``module_filter`` as an
                AND clause. ``None`` (default) searches every schema —
                preserves the pre-Phase-3 multi-schema-blind behaviour.

        Returns:
            List of result dicts with function_name, module, description,
            tables_read, tables_written, key_columns, schema, and score.
        """
        if not self._client:
            logger.warning("VectorStore unavailable — returning empty results")
            return []

        try:
            blob = self._float_list_to_bytes(query_embedding)
            filter_clause = self._build_filter_clause(
                module_filter=module_filter,
                schema_filter=schema_filter,
            )
            q = (
                Query(f"({filter_clause})=>[KNN {top_k} @embedding $vec AS score]")
                .sort_by("score")
                .return_fields(
                    "function_name", self.SCHEMA_FIELD, "module", "description",
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
                    "schema": self._decode(getattr(doc, self.SCHEMA_FIELD, "")),
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

    @staticmethod
    def _build_filter_clause(
        module_filter: Optional[str],
        schema_filter: Optional[str],
    ) -> str:
        """Build the RediSearch pre-filter clause for KNN.

        Empty filter is ``*`` (match-all). Single TAG filter is the
        ``@field:{value}`` form. Two TAG filters combine via space (AND).
        """
        clauses = []
        if schema_filter:
            clauses.append(f"@schema:{{{schema_filter}}}")
        if module_filter:
            clauses.append(f"@module:{{{module_filter}}}")
        if not clauses:
            return "*"
        return " ".join(clauses)

    async def get_function_doc(
        self, schema: str, function_name: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a function's indexed document.

        Args:
            schema: Oracle owner the function belongs to (Phase 3:
                replaces the pre-Phase-3 ``module`` argument that named
                a doc-key segment).
            function_name: PL/SQL function name.

        Returns:
            Dict of stored fields, or None if not found.
        """
        if not self._client:
            return None
        try:
            key = self._doc_key(schema, function_name)
            data = await self._client.hgetall(key)
            if not data:
                return None
            return {k.decode(): v.decode() for k, v in data.items() if k != b"embedding"}
        except Exception as exc:
            logger.warning(f"Failed to get doc {schema}:{function_name}: {exc}")
            return None

    async def delete_function(self, schema: str, function_name: str) -> bool:
        """Delete a function from the vector index.

        Args:
            schema: Oracle owner (Phase 3: doc-key segment).
            function_name: PL/SQL function name.

        Returns:
            True if deleted, False on error.
        """
        if not self._client:
            return False
        try:
            key = self._doc_key(schema, function_name)
            await self._client.delete(key)
            logger.info(f"Deleted vector doc: {schema}:{function_name}")
            return True
        except Exception as exc:
            logger.warning(f"Failed to delete {schema}:{function_name}: {exc}")
            return False

    async def list_indexed_functions(
        self, schema: Optional[str] = None
    ) -> List[str]:
        """List all indexed function names.

        Args:
            schema: Optional Oracle-owner filter (Phase 3: replaces
                ``module``).

        Returns:
            List of function names.
        """
        if not self._client:
            return []
        try:
            pattern = f"{self.KEY_PREFIX}{schema}:*" if schema else f"{self.KEY_PREFIX}*"
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

    def _doc_key(self, schema: str, function_name: str) -> str:
        """Build a Redis key for a function document.

        Phase 3: the second segment is the Oracle owner (``OFSMDM``,
        ``OFSERM``, …). Pre-Phase-3 it was the module name.

        Args:
            schema: Oracle owner (e.g. ``OFSMDM``).
            function_name: PL/SQL function name.

        Returns:
            Redis key string.
        """
        return f"{self.KEY_PREFIX}{schema}:{function_name}"

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
