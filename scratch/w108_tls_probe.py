"""W108 / W122b TLS-rationale probe.

Calls gpt-4o-mini with three padded prompt sizes (3000 / 8000 / 12000 chars)
from the same machine + network egress as the indexer runs on. Records
success / error / latency for each size so we can verify whether the
"corporate TLS payload limit" rationale in indexer.py:209 is real,
partially real, or stale.

Mirrors the indexer's call shape: provider=openai, model=gpt-4o-mini,
SystemMessage + HumanMessage with PL/SQL source body, minimal completion.
"""

from __future__ import annotations

import os
import sys
import time

# Match the runtime the indexer uses (.env.dev loaded by cli.py / main.py).
from dotenv import load_dotenv

load_dotenv(".env.dev")

sys.path.insert(0, "src")

from langchain_core.messages import SystemMessage, HumanMessage  # noqa: E402

from llm_factory import create_llm  # noqa: E402


SIZES = [3000, 8000, 12000]

# Minimal system prompt — we're testing request acceptance, not quality.
SYSTEM_PROMPT = "Summarize the following PL/SQL source in one short sentence."

# A realistic-looking PL/SQL fragment to seed the pad. Repeated to hit
# target size. Indexer sends real PL/SQL; padding with PL/SQL-shaped text
# keeps tokenization realistic vs lorem ipsum.
SEED = (
    "CREATE OR REPLACE PROCEDURE OFSERM.SAMPLE_FN (p_run_id IN NUMBER) IS\n"
    "BEGIN\n"
    "    INSERT INTO FCT_STANDARD_ACCT_HEAD (n_run_skey, v_std_acct_head_id, n_amount)\n"
    "    SELECT p_run_id, 'CAP170', SUM(n_balance)\n"
    "    FROM   STG_LOAN_EXPOSURE\n"
    "    WHERE  d_mis_date = TO_DATE('31-DEC-2024', 'DD-MON-YYYY')\n"
    "    GROUP  BY v_loan_id;\n"
    "    COMMIT;\n"
    "END SAMPLE_FN;\n"
    "/\n\n"
)


def make_padded_body(target_chars: int) -> str:
    body = SEED
    while len(body) < target_chars:
        body += SEED
    return body[:target_chars]


def probe(target_chars: int) -> dict:
    body = make_padded_body(target_chars)
    user = (
        f"Function Name: SAMPLE_FN\n\nSource Code:\n{body}"
    )
    llm = create_llm(
        provider="openai",
        model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
        site="w108_tls_probe",
        temperature=0,
        max_tokens=50,
        json_mode=False,
    )
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user)]

    t0 = time.perf_counter()
    try:
        resp = llm.invoke(messages)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        content = (resp.content or "")[:120]
        return {
            "chars": target_chars,
            "ok": True,
            "elapsed_ms": round(elapsed_ms, 1),
            "first_response_snippet": content,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "chars": target_chars,
            "ok": False,
            "elapsed_ms": round(elapsed_ms, 1),
            "first_response_snippet": None,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:400],
        }


def main() -> None:
    import json

    results = []
    for size in SIZES:
        print(f"[probe] size={size} chars ...", flush=True)
        r = probe(size)
        results.append(r)
        print(json.dumps(r, indent=2), flush=True)
        print("---", flush=True)
    print("\n[summary]")
    for r in results:
        status = "OK" if r["ok"] else f"FAIL ({r.get('error_type')})"
        print(f"  {r['chars']:>6} chars  {r['elapsed_ms']:>8.1f} ms  {status}")


if __name__ == "__main__":
    main()
