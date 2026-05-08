"""Unit tests for src/llm_factory.py — W34c Phase 1.

Covers the per-call-site model dispatch:
  - SITE_MODEL_DEFAULTS lookups return the expected model
  - explicit model= wins over site=
  - unknown site falls back to the global default
  - RTIE_MODEL_OVERRIDES applies on top of defaults
  - malformed RTIE_MODEL_OVERRIDES JSON is ignored
  - drift assertion: every site key in SITE_MODEL_DEFAULTS is referenced
    by exactly one create_llm(site=...) call in src/, and vice versa
"""

from __future__ import annotations

import ast
import importlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import src.llm_factory as llm_factory_mod
from src.llm_factory import (
    SITE_MODEL_DEFAULTS,
    create_llm,
    get_default_model,
    get_site_model,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _reload_factory() -> None:
    """Reload the module so RTIE_MODEL_OVERRIDES env changes take effect."""
    importlib.reload(llm_factory_mod)


@pytest.fixture
def fake_openai_key(monkeypatch):
    """Provide a fake OPENAI_API_KEY so create_llm doesn't raise."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")


# ---------------------------------------------------------------------
# Site lookup
# ---------------------------------------------------------------------

@pytest.mark.parametrize("site_key,expected_model", list(SITE_MODEL_DEFAULTS.items()))
def test_site_lookup_returns_expected_default(
    fake_openai_key, monkeypatch, site_key, expected_model
):
    """For each registered site, create_llm(site=key) configures that model."""
    monkeypatch.delenv("RTIE_MODEL_OVERRIDES", raising=False)
    _reload_factory()
    from src.llm_factory import create_llm as create_llm_fresh

    captured = {}

    real_chat_openai = llm_factory_mod.ChatOpenAI

    def _spy(**kwargs):
        captured.update(kwargs)
        return object()  # don't actually instantiate the real client

    with patch.object(llm_factory_mod, "ChatOpenAI", _spy):
        create_llm_fresh(provider="openai", site=site_key)

    assert captured.get("model") == expected_model, (
        f"site={site_key!r} resolved to {captured.get('model')!r}, "
        f"expected {expected_model!r}"
    )


def test_get_site_model_returns_none_for_unknown_site():
    assert get_site_model("not.a.real.site") is None


def test_get_site_model_returns_known_site():
    assert get_site_model("indexer.generate_description") == "gpt-4o-mini"


# ---------------------------------------------------------------------
# W34c Phase 2: explicit pinning for the newly promoted site.
# Duplicates coverage from the parametrized test above, but pins the
# Phase 2 site by name so an accidental SITE_MODEL_DEFAULTS removal
# still fails a clearly-named test rather than just dropping a
# parametrize case silently. classify_query was originally part of
# Phase 2 but reverted after C14 caught a ClassificationResult
# ValidationError on gpt-4o-mini reconciliation queries.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "site_key,expected_model",
    [
        ("phase2.explainer.invoke", "gpt-4o-mini"),
    ],
)
def test_phase2_sites_resolve_to_gpt_4o_mini(site_key, expected_model):
    assert SITE_MODEL_DEFAULTS[site_key] == expected_model
    assert get_site_model(site_key) == expected_model


# ---------------------------------------------------------------------
# W34c Phase 3: explicit pinning for data_query._generate_sql.
# Same shape as the Phase 2 pin above so an accidental
# SITE_MODEL_DEFAULTS removal fails with a clearly-named test rather
# than silently dropping a parametrize case. Tier 2 canary (5/5,
# hand-verified row counts and aggregates) is the load-bearing
# end-to-end gate.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "site_key,expected_model",
    [
        ("data_query._generate_sql", "gpt-4o-mini"),
    ],
)
def test_phase3_sites_resolve_to_gpt_4o_mini(site_key, expected_model):
    assert SITE_MODEL_DEFAULTS[site_key] == expected_model
    assert get_site_model(site_key) == expected_model


# ---------------------------------------------------------------------
# Explicit model wins over site=
# ---------------------------------------------------------------------

def test_explicit_model_arg_wins_over_site(fake_openai_key, monkeypatch):
    monkeypatch.delenv("RTIE_MODEL_OVERRIDES", raising=False)
    _reload_factory()
    from src.llm_factory import create_llm as create_llm_fresh

    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return object()

    with patch.object(llm_factory_mod, "ChatOpenAI", _spy):
        create_llm_fresh(
            provider="openai",
            site="variable_tracer.stream_chain",  # site default would be gpt-4o-mini
            model="gpt-5",
        )

    assert captured["model"] == "gpt-5"


# ---------------------------------------------------------------------
# Unknown site falls back to global default
# ---------------------------------------------------------------------

def test_unknown_site_falls_back_to_global_default(
    fake_openai_key, monkeypatch, caplog
):
    monkeypatch.delenv("RTIE_MODEL_OVERRIDES", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-mini")
    _reload_factory()
    from src.llm_factory import create_llm as create_llm_fresh, logger as factory_logger

    # Custom logger has propagate=False; flip for the duration of the test
    # so caplog (which attaches at root) sees records.
    monkeypatch.setattr(factory_logger, "propagate", True)

    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return object()

    with patch.object(llm_factory_mod, "ChatOpenAI", _spy):
        with caplog.at_level("WARNING", logger="src.llm_factory"):
            create_llm_fresh(provider="openai", site="bogus.site.key")

    assert captured["model"] == "gpt-5-mini"
    assert any("bogus.site.key" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------
# RTIE_MODEL_OVERRIDES env var
# ---------------------------------------------------------------------

def test_env_overrides_apply(fake_openai_key, monkeypatch):
    monkeypatch.setenv(
        "RTIE_MODEL_OVERRIDES",
        '{"variable_tracer.stream_chain": "gpt-5-mini"}',
    )
    _reload_factory()
    fresh_mod = importlib.import_module("src.llm_factory")

    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return object()

    with patch.object(fresh_mod, "ChatOpenAI", _spy):
        fresh_mod.create_llm(
            provider="openai",
            site="variable_tracer.stream_chain",
        )

    assert captured["model"] == "gpt-5-mini"

    # Sites NOT in the override still use the default.
    captured.clear()
    with patch.object(fresh_mod, "ChatOpenAI", _spy):
        fresh_mod.create_llm(
            provider="openai",
            site="variable_tracer.explain_chain",
        )
    assert captured["model"] == "gpt-4o-mini"


def test_env_overrides_malformed_json_ignored(
    fake_openai_key, monkeypatch, caplog
):
    monkeypatch.setenv("RTIE_MODEL_OVERRIDES", "{not valid json")
    # Flip propagate before reload so the warning emitted during module
    # load is visible to caplog (root handler).
    import logging
    factory_logger = logging.getLogger("src.llm_factory")
    monkeypatch.setattr(factory_logger, "propagate", True)
    with caplog.at_level("WARNING", logger="src.llm_factory"):
        _reload_factory()
    fresh_mod = importlib.import_module("src.llm_factory")
    # Re-flip after reload — reload reuses the same logger object, so
    # propagate is preserved, but be defensive.
    monkeypatch.setattr(fresh_mod.logger, "propagate", True)

    # Defaults are unchanged; warning was logged.
    assert (
        fresh_mod.SITE_MODEL_DEFAULTS["variable_tracer.stream_chain"]
        == "gpt-4o-mini"
    )
    assert any(
        "RTIE_MODEL_OVERRIDES" in rec.message
        and "not valid JSON" in rec.message
        for rec in caplog.records
    )

    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return object()

    with patch.object(fresh_mod, "ChatOpenAI", _spy):
        fresh_mod.create_llm(
            provider="openai",
            site="variable_tracer.stream_chain",
        )
    assert captured["model"] == "gpt-4o-mini"


def test_env_overrides_non_object_json_ignored(fake_openai_key, monkeypatch):
    monkeypatch.setenv("RTIE_MODEL_OVERRIDES", '["not", "an", "object"]')
    _reload_factory()
    fresh_mod = importlib.import_module("src.llm_factory")
    assert (
        fresh_mod.get_site_model("variable_tracer.stream_chain")
        == "gpt-4o-mini"
    )


# ---------------------------------------------------------------------
# Drift assertion (STEP 3 option b): walk src/ for create_llm(site=...)
# calls and assert the set of literal keys equals SITE_MODEL_DEFAULTS.keys().
# ---------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


def _collect_create_llm_site_keys() -> set[str]:
    """AST-walk src/ for create_llm(...) calls and return all literal site=
    string values. Sites passed dynamically (e.g. site=some_var) are
    skipped silently — none exist today, and adding one without a literal
    would defeat the drift check anyway.
    """
    found: set[str] = set()
    for py_path in SRC_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Match both `create_llm(...)` and `llm_factory.create_llm(...)`.
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "create_llm":
                continue
            for kw in node.keywords:
                if kw.arg != "site":
                    continue
                value = kw.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(value.value)
    return found


def test_drift_site_keys_match_site_model_defaults():
    """Every key in SITE_MODEL_DEFAULTS is referenced by at least one
    create_llm(site=...) call in src/, and every literal site= value in
    src/ is a key in SITE_MODEL_DEFAULTS.
    """
    referenced = _collect_create_llm_site_keys()
    declared = set(SITE_MODEL_DEFAULTS.keys())

    missing_in_src = declared - referenced
    unknown_in_src = referenced - declared

    assert not missing_in_src, (
        f"SITE_MODEL_DEFAULTS keys with no create_llm(site=...) caller "
        f"in src/: {sorted(missing_in_src)}"
    )
    assert not unknown_in_src, (
        f"create_llm(site=...) calls referencing keys not in "
        f"SITE_MODEL_DEFAULTS: {sorted(unknown_in_src)}"
    )
