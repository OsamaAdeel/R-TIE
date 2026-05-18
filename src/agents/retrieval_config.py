"""W80b — Per-query-type top-K config for vector retrieval.

Pre-W80b the vector search call hardcoded ``top_k=5`` at every site.
That floor was tight enough that semantic clusters with more than 5
functions (the significant-investment cluster has 15 in OFSERM) lost
the rank-6+ candidates to the KNN cutoff regardless of embedding
quality. The W80 v1 canary surfaced this as a MEDIUM recall floor:
2 of 5 target functions retrieved, 3 close siblings beating one
target out of the top-5.

W80b raises ``top_k`` for query types that legitimately benefit from
recall — multi-stage chain traces and column-writer enumerations —
while keeping FUNCTION_LOGIC narrow because that route is anchored
upstream (W76 / BI routing / W87) and extra candidates only add
narrative-LLM noise.

This is the tactical bridge. The architecturally correct destination
is W80c (hybrid graph + vector with rerank), gated on W36/W88 graph-
aware retrieval preconditions.

Cost note: embedding cost is per-query, not per-result, so a higher
top_k doesn't multiply embedding API calls. RediSearch KNN cost
scales with top_k but in microseconds at this corpus size (~178 docs);
the per-schema fan-out from W79 means the actual returned set size
can be roughly ``2 * top_k`` for ALL-scope queries (one hit-list per
discovered schema, then merged) — bounded and acceptable.
"""

from __future__ import annotations

from typing import Optional


# Per-query-type top-K. Lookup is via dict.get() with a default; an
# unknown query_type (legitimate during W-ticket additions) falls
# through to W80B_DEFAULT_TOP_K rather than raising KeyError.
#
# Rationale per row:
#   FUNCTION_LOGIC        — anchored upstream by W76 or declined by
#                            W87; one function is the answer.
#                            Extras only add narrative-LLM noise.
#   COLUMN_LOGIC          — a column can have many writers in a
#                            dense schema; raise to surface the
#                            full writer set.
#   VARIABLE_TRACE        — multi-stage chain traces visit many
#                            functions; 20 is the floor that covers
#                            the 15-function significant-investment
#                            cluster with headroom.
#   VALUE_TRACE           — Phase 2 row-first path; vector search is
#                            advisory at best, not load-bearing.
#   DIFFERENCE_EXPLANATION — Phase 2 row-first path; same.
#   DATA_QUERY            — Option A; uses schema catalogs and SQL
#                            generation, not vector retrieval.
#   UNSUPPORTED           — short-circuits before vector search.
W80B_TOP_K_BY_QUERY_TYPE: dict[str, int] = {
    "FUNCTION_LOGIC": 5,
    "COLUMN_LOGIC": 15,
    "VARIABLE_TRACE": 20,
    "VALUE_TRACE": 5,
    "DIFFERENCE_EXPLANATION": 5,
    "DATA_QUERY": 5,
    "UNSUPPORTED": 5,
}

W80B_DEFAULT_TOP_K: int = 5


def resolve_top_k(query_type: Optional[str]) -> int:
    """Return the configured top-K for *query_type*, or the default.

    Uses ``dict.get()`` so unknown / empty / None query_type values
    degrade to :data:`W80B_DEFAULT_TOP_K` rather than raising
    ``KeyError`` — important during W-ticket evolutions where a new
    query_type may land in the classifier before the config is
    updated, and during early-stream branches where ``state["query_type"]``
    may not yet be populated.
    """
    if not query_type:
        return W80B_DEFAULT_TOP_K
    return W80B_TOP_K_BY_QUERY_TYPE.get(query_type, W80B_DEFAULT_TOP_K)
