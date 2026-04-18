"""
Pick a trace strategy based on row origin.

The router is the single place where Phase 2 decides *how* to explain
a row. It does not itself build evidence -- that is delegated to the
EvidenceBuilder.
"""

from __future__ import annotations


class TraceRouter:
    """Map an origin classification to a trace strategy name."""

    # (origin_category, traceable) -> strategy
    ROUTES: dict[tuple[str, bool], str] = {
        ("PLSQL", True):  "graph_trace",
        ("PLSQL", False): "partial_graph_trace",
        ("ETL",   False): "etl_explain",
        ("UNKNOWN", False): "unknown_origin_diagnose",
    }

    def decide(self, classification: dict) -> str:
        """Return the strategy name for *classification*.

        The classification dict comes straight from
        :class:`OriginClassifier.classify_row`.
        """
        category = classification.get("origin_category", "UNKNOWN")
        traceable = bool(classification.get("traceable_via_graph"))
        return self.ROUTES.get((category, traceable), "unknown_origin_diagnose")

    def route(
        self,
        classification: dict,
        row: dict,
        filters: dict,
    ) -> dict:
        """Compute a route decision for the caller.

        Returns a lightweight descriptor. The caller then invokes the
        EvidenceBuilder using the ``strategy`` value.
        """
        strategy = self.decide(classification)
        traceable_strategies = {"graph_trace", "partial_graph_trace"}
        return {
            "strategy": strategy,
            "can_explain": strategy in traceable_strategies
                           or strategy in {"etl_explain", "unknown_origin_diagnose"},
            "origin_category": classification.get("origin_category"),
            "origin_value": classification.get("origin_value"),
        }
