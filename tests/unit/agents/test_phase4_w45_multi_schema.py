"""Phase 4 — W45 detector multi-schema backstop.

``detect_ungrounded_identifiers`` previously decided ungroundedness from
the multi_source dict alone — accurate when semantic search reached
every schema, but vulnerable to false positives when an OFSERM function
owning the identifier wasn't in the top-K retrieval. Phase 4 adds an
optional ``redis_client`` parameter; when provided, the detector also
scans ``graph:source:<schema>:*`` across every discovered schema and
treats an identifier as grounded if found anywhere.

These tests pin both the pre-Phase-4 behaviour (no redis_client = local
check only) and the Phase-4 enhancement (redis_client supplied = global
backstop applies).
"""

from __future__ import annotations

from src.agents.logic_explainer import (
    detect_ungrounded_identifiers,
    evaluate_grounding,
)
from src.parsing.serializer import to_msgpack


class _FakeRedis:
    def __init__(self, storage: dict[str, bytes] | None = None) -> None:
        self._storage: dict[str, bytes] = dict(storage or {})

    def keys(self, pattern: str) -> list[bytes]:
        if isinstance(pattern, bytes):
            pattern = pattern.decode()
        prefix = pattern.rstrip("*")
        return [
            k.encode() for k in self._storage.keys() if k.startswith(prefix)
        ]

    def get(self, key) -> bytes | None:
        if isinstance(key, bytes):
            key = key.decode()
        return self._storage.get(key)

    def set(self, key, value) -> None:
        if isinstance(key, bytes):
            key = key.decode()
        if isinstance(value, str):
            value = value.encode()
        self._storage[key] = value

    def scan(self, cursor: int = 0, match: str | None = None, count: int = 500):
        if match is None:
            return (0, [k.encode() for k in self._storage.keys()])
        if isinstance(match, bytes):
            match = match.decode()
        prefix = match.rstrip("*")
        matches = [
            k.encode() for k in self._storage.keys() if k.startswith(prefix)
        ]
        return (0, matches)


def _multi_source_with_text(text: str) -> dict:
    """Build a multi_source dict whose source_code contains *text*."""
    return {
        "FN_X": {
            "source_code": [{"line": 1, "text": text}],
            "score": 0.5,
        }
    }


# ---------------------------------------------------------------------
# Pre-Phase-4 behaviour — no redis_client, no backstop
# ---------------------------------------------------------------------


def test_pre_phase4_flags_when_identifier_missing_from_multi_source():
    multi = _multi_source_with_text("BEGIN NULL; END;")
    out = detect_ungrounded_identifiers(
        raw_query="How is CAP973 calculated?",
        multi_source=multi,
    )
    assert out == ["CAP973"]


def test_pre_phase4_silent_when_identifier_present_in_multi_source():
    multi = _multi_source_with_text("WHERE V_STD_ACCT_HEAD_ID = 'CAP943'")
    out = detect_ungrounded_identifiers(
        raw_query="How is CAP943 calculated?",
        multi_source=multi,
    )
    assert out == []


def test_pre_phase4_no_identifiers_in_query_returns_empty():
    out = detect_ungrounded_identifiers(
        raw_query="Explain something",
        multi_source={},
    )
    assert out == []


# ---------------------------------------------------------------------
# Phase 4 — redis_client provided, multi-schema backstop applies
# ---------------------------------------------------------------------


def test_phase4_suppresses_ungrounded_when_identifier_in_other_schema_source():
    """multi_source missed it, but graph:source:OFSERM has it → silent."""
    fake = _FakeRedis()
    fake.set("graph:OFSERM:FN_OWNER", to_msgpack({"function": "FN_OWNER"}))
    fake.set(
        "graph:source:OFSERM:FN_OWNER",
        to_msgpack(["WHERE V_STD_ACCT_HEAD_ID = 'CAP943'"]),
    )
    multi = _multi_source_with_text("BEGIN NULL; END;")

    out = detect_ungrounded_identifiers(
        raw_query="How is CAP943 calculated?",
        multi_source=multi,
        redis_client=fake,
    )
    assert out == []


def test_phase4_still_fires_when_truly_absent_everywhere():
    """No source body anywhere mentions the identifier → still ungrounded."""
    fake = _FakeRedis()
    fake.set("graph:OFSMDM:FN_A", to_msgpack({"function": "FN_A"}))
    fake.set(
        "graph:source:OFSMDM:FN_A",
        to_msgpack(["BEGIN", "  NULL;", "END;"]),
    )
    multi = _multi_source_with_text("ALSO_DOESNT_HAVE_IT;")

    out = detect_ungrounded_identifiers(
        raw_query="How is CAP999 calculated?",
        multi_source=multi,
        redis_client=fake,
    )
    assert out == ["CAP999"]


def test_phase4_grounding_evaluator_uses_backstop_too():
    """``evaluate_grounding`` mirrors the same multi-schema backstop so
    the post-hoc warning matches the pre-generation routing decision."""
    fake = _FakeRedis()
    fake.set("graph:OFSERM:FN_OWNER", to_msgpack({"function": "FN_OWNER"}))
    fake.set(
        "graph:source:OFSERM:FN_OWNER",
        to_msgpack(["WHERE V_STD_ACCT_HEAD_ID = 'CAP943'"]),
    )
    multi = _multi_source_with_text("a function body without the identifier")

    grounding = evaluate_grounding(
        raw_query="How is CAP943 calculated?",
        markdown="## CAP943\nSome explanation citing Lines 5-10.",
        multi_source=multi,
        functions_analyzed=["FN_X"],
        query_type="VARIABLE_TRACE",
        redis_client=fake,
    )
    # No UNGROUNDED warning since the identifier exists in another schema.
    assert not any(
        w.startswith("UNGROUNDED_IDENTIFIERS") for w in grounding["warnings"]
    )


def test_phase4_grounding_evaluator_still_fires_for_absent_identifier():
    fake = _FakeRedis()
    fake.set("graph:OFSMDM:FN_A", to_msgpack({"function": "FN_A"}))
    fake.set("graph:source:OFSMDM:FN_A", to_msgpack(["BEGIN NULL; END;"]))
    multi = _multi_source_with_text("nothing here either")

    grounding = evaluate_grounding(
        raw_query="How is CAP999 calculated?",
        markdown="## CAP999\nSome explanation citing Lines 5-10.",
        multi_source=multi,
        functions_analyzed=["FN_X"],
        query_type="VARIABLE_TRACE",
        redis_client=fake,
    )
    assert any(
        w.startswith("UNGROUNDED_IDENTIFIERS") for w in grounding["warnings"]
    )
