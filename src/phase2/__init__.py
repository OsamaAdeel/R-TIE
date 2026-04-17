"""
Phase 2 — Value Lineage & Data Trace.

Reuses Phase 1 graph representation and adds a data layer that fetches
actual values at each graph node, assembles a proof chain, and
identifies where value discrepancies originate.

All Oracle queries are read-only SELECT statements, validated by
SQLGuardian, and parameterised via bind variables.
"""
