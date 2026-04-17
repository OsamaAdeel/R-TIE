"""
Phase 2 integration validation script.

Run against a live Redis (with graph indexed) + Oracle. Requires the
environment variables used by the main app (.env.dev loaded) and an
OpenAI API key for the LLM narration step.

Usage:
    python validate_phase2.py

When Oracle or OpenAI is unavailable the script still exercises the
graph-resolve + proof-chain pipeline and reports which steps returned
empty rows.
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

from src.agents.value_tracer import ValueTracerAgent
from src.tools.schema_tools import SchemaTools
from src.tools.sql_guardian import SQLGuardian


async def _main() -> int:
    env = os.getenv("ENVIRONMENT", "dev")
    load_dotenv(f".env.{env}")

    try:
        import redis as _redis
        redis_client = _redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
        )
        redis_client.ping()
    except Exception as exc:
        print(f"[FAIL] Redis not reachable: {exc}")
        return 1

    schema_tools = SchemaTools(
        host=os.getenv("ORACLE_HOST"),
        port=int(os.getenv("ORACLE_PORT", "1521")),
        sid=os.getenv("ORACLE_SID"),
        user=os.getenv("ORACLE_USER"),
        password=os.getenv("ORACLE_PASSWORD"),
        pool_min=1,
        pool_max=2,
    )
    oracle_ok = True
    try:
        await schema_tools.initialize()
    except Exception as exc:
        print(f"[warn] Oracle not reachable: {exc}")
        oracle_ok = False

    agent = ValueTracerAgent(
        schema_tools=schema_tools if oracle_ok else _OracleStub(),
        redis_client=redis_client,
        sql_guardian=SQLGuardian(),
    )

    print("=== Test 1: VALUE_TRACE ===")
    result = await agent.trace_value(
        target_variable="N_ANNUAL_GROSS_INCOME",
        filters={
            "mis_date": "2025-12-31",
            "lob_code": "CBA",
            "lv_code": "ABL",
        },
        schema="OFSMDM",
        user_query="How is N_ANNUAL_GROSS_INCOME calculated?",
    )
    assert result["query_type"] == "VALUE_TRACE", "query_type must be VALUE_TRACE"
    assert result["proof_chain"] is not None, "proof_chain must be populated"
    assert result["verification_sql"] is not None, "verification_sql must be present"
    print(f"  steps:           {len(result['proof_chain']['steps'])}")
    print(f"  confidence:      {result['confidence']}")
    print(f"  final_value:     {result['proof_chain'].get('final_value')}")
    print(f"  explanation:     {result['explanation'][:200]}...")

    print()
    print("=== Test 2: DIFFERENCE_EXPLANATION ===")
    result = await agent.explain_difference(
        target_variable="N_ANNUAL_GROSS_INCOME",
        filters={"mis_date": "2025-12-31", "lob_code": "CBA", "lv_code": "ABL"},
        schema="OFSMDM",
        bank_value=1_000_000.0,
        system_value=980_000.0,
        user_query="Bank says 1M, system shows 980k -- why?",
    )
    assert result["query_type"] == "DIFFERENCE_EXPLANATION"
    delta = result.get("delta_analysis")
    if delta is None:
        print("  [warn] no delta_analysis produced (likely no data available)")
    else:
        print(f"  cause_type:     {delta.get('cause_type')}")
        print(f"  root_cause:     step {delta.get('root_cause_step')}")
        print(f"  explanation:    {delta.get('explanation')}")

    print()
    print("[OK] Phase 2 validation complete")
    return 0


class _OracleStub:
    """Minimal schema_tools stand-in when Oracle is not available."""

    async def execute_raw(self, sql, params):  # noqa: D401
        return []


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
