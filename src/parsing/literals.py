"""
Business identifier literal extraction (W35 Phase 5).

Scans function source bodies for string literals that match configured
business-identifier patterns (e.g. ``CAP\\d{3}`` for Basel capital
standard account head IDs) and classifies each occurrence by SQL role:

  - ``filter``           — RHS of ``=`` in a WHERE / JOIN ON clause
  - ``case_when_source`` — RHS of ``=`` inside a CASE WHEN whose THEN
                           branch returns an amount-like column reference
  - ``case_when_target`` — RHS of ``=`` inside a CASE WHEN whose THEN
                           branch returns a small integer flag (10, 11, …)
  - ``in_list_member``   — member of a comma-separated IN-list of literals

Only matches that appear *inside* single-quoted SQL string literals are
extracted. The matcher does NOT touch non-string occurrences (column
names, table names, comment text — comments are stripped upstream).

This module does NOT mutate state — see ``loader.py`` for the integration
point that persists the per-schema index at ``graph:literal:<schema>:<id>``.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from src.logger import get_logger

logger = get_logger(__name__, concern="app")


# ---------------------------------------------------------------------------
# Default patterns
# ---------------------------------------------------------------------------

# Default business identifier pattern set. Mirrors the default block in
# config/settings.yaml so unit tests and offline tools can build the
# extractor without loading YAML.
DEFAULT_BUSINESS_IDENTIFIER_PATTERNS: dict[str, dict[str, str]] = {
    "cap_codes": {
        "regex": r"CAP\d{3}",
        "description": "Basel capital standard account head IDs",
    },
}


# A compiled "outer" matcher anchors each pattern inside SQL single-quote
# string literals. The captured group is the identifier text itself, e.g.
# ``'CAP943'`` matches and the captured group is ``CAP943``.
def _wrap_in_string_literal(pattern: str) -> str:
    """Return a regex that matches *pattern* inside ``'...'`` SQL literals.

    The wrap anchors to a single-quote on each side and captures the
    identifier as group 1. The single quotes are NOT included in the
    captured group, so callers receive the bare identifier string.
    """
    return r"'(" + pattern + r")'"


# ---------------------------------------------------------------------------
# Pattern compilation
# ---------------------------------------------------------------------------

class CompiledPattern:
    """A single business identifier pattern after validation + compile.

    Attributes
    ----------
    name:        The config key (e.g. ``cap_codes``). Used for logging /
                 future role-prefixing only.
    raw_regex:   The user-supplied regex (without quote-wrapping).
    bare:        ``re.Pattern`` for the bare identifier (no quote anchors).
                 Used by tests and the validator helper.
    quoted:      ``re.Pattern`` for the identifier inside ``'...'`` SQL
                 literals. Used during extraction.
    description: Optional human description from config.
    """

    __slots__ = ("name", "raw_regex", "bare", "quoted", "description")

    def __init__(self, name: str, raw_regex: str, description: str = "") -> None:
        self.name = name
        self.raw_regex = raw_regex
        # Anchor the bare matcher so it matches the WHOLE token. This is what
        # callers use to validate "does X match my pattern" — partial matches
        # on substrings (e.g. matching CAP1 inside CAP123) are not desired.
        self.bare = re.compile(r"\A" + raw_regex + r"\Z")
        # The quoted matcher is intentionally NOT line-anchored — the SQL is
        # often a single physical line containing dozens of literals.
        self.quoted = re.compile(_wrap_in_string_literal(raw_regex))
        self.description = description


def compile_patterns(
    config: dict[str, Any] | None,
) -> list[CompiledPattern]:
    """Compile patterns from a config dict.

    Accepts the ``business_identifier_patterns`` block from settings.yaml
    (or any equivalently-shaped dict). Each entry must have a ``regex``
    field; ``description`` is optional. Invalid entries are logged and
    skipped — extraction continues with whatever entries DID compile so a
    typo in one pattern doesn't disable the rest.

    When *config* is ``None`` or empty, falls back to
    ``DEFAULT_BUSINESS_IDENTIFIER_PATTERNS``.

    Returns a list of :class:`CompiledPattern`. Empty list means no
    patterns to extract — extraction becomes a no-op.
    """
    source: dict[str, dict[str, str]]
    if not config:
        source = DEFAULT_BUSINESS_IDENTIFIER_PATTERNS
    else:
        source = config

    compiled: list[CompiledPattern] = []
    for name, entry in source.items():
        if not isinstance(entry, dict):
            logger.warning(
                "business_identifier_patterns.%s: expected a dict, got %s — "
                "skipping",
                name, type(entry).__name__,
            )
            continue
        regex_str = entry.get("regex")
        if not isinstance(regex_str, str) or not regex_str:
            logger.warning(
                "business_identifier_patterns.%s: missing or empty 'regex' — "
                "skipping",
                name,
            )
            continue
        try:
            compiled.append(
                CompiledPattern(
                    name=name,
                    raw_regex=regex_str,
                    description=entry.get("description", "") or "",
                )
            )
        except re.error as exc:
            logger.warning(
                "business_identifier_patterns.%s: invalid regex %r (%s) — "
                "skipping",
                name, regex_str, exc,
            )
            continue
    return compiled


# ---------------------------------------------------------------------------
# Role classification
# ---------------------------------------------------------------------------

# Heuristic constants. None are user-tunable yet — Phase 6/7 will revisit.

# Window before the literal we examine when looking for an enclosing CASE
# WHEN. OFSAA-generated SQL often has very long single-line statements,
# so this window must be generous.
_CASE_WINDOW_BEFORE = 800

# Window after the literal we examine to find the matching THEN branch.
# CS_Regulatory_Adjustments_Phase_In_Deduction_Amount has eight CAP-codes
# OR-chained inside one CASE WHEN, with the matching THEN ~600 chars
# past the first literal — this constant must clear that bar so the
# earliest literals in such chains still classify as case_when_target.
_THEN_WINDOW_AFTER = 1200

# Pattern matching small integer flag results that distinguish a target
# CASE WHEN (decides which arm of a calculation to use, returning 10/11
# in OFSAA's COND_xxx convention) from a source CASE WHEN (selects an
# amount column).
_INT_FLAG_RE = re.compile(r"^-?\d{1,4}\.?\d*$")

# Amount-like column references suggest case_when_source. Matches things
# like ``n_std_acct_head_amt``, ``CAPITAL_ACCOUNTING.n_phase_in_amt``,
# ``SOMETHING_AMOUNT``, ``balance_amt``, etc. The leading word boundary
# is important to avoid false positives on ``standard_acct_head_amount``-
# inside-a-comment-style noise.
_AMOUNT_TOKEN_RE = re.compile(
    r"\b(?:[a-z_][a-z0-9_]*_amt|[a-z_][a-z0-9_]*_amount|amount|balance)\b",
    re.IGNORECASE,
)

# Used to detect "IN (...subquery...)" — when the open-paren that
# encloses our literal contains a SELECT, we're in a subquery, NOT in an
# IN-list of literals.
_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
_IN_TAIL_RE = re.compile(r"\bIN\s*$", re.IGNORECASE)
_END_RE = re.compile(r"\bEND\b", re.IGNORECASE)
_WHEN_RE = re.compile(r"\bWHEN\b", re.IGNORECASE)
_THEN_BRANCH_RE = re.compile(
    r"\bTHEN\b\s+(.+?)(?=\s+\bELSE\b|\s+\bWHEN\b|\s+\bEND\b)",
    re.IGNORECASE | re.DOTALL,
)


def _find_enclosing_open_paren(text: str, lit_start: int) -> int | None:
    """Walk backwards from *lit_start* and return the index of the nearest
    unmatched ``(``, or ``None`` if there is no enclosing paren.

    "Unmatched" means the depth is 0 *relative to lit_start* — i.e. the
    paren opens a scope that the literal currently sits inside. The walk
    stops at index 0 if no such paren is found.
    """
    depth = 0
    i = lit_start - 1
    while i >= 0:
        c = text[i]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                return i
            depth -= 1
        i -= 1
    return None


def classify_role(text: str, lit_start: int, lit_end: int) -> str:
    """Classify the role of a literal at ``text[lit_start:lit_end]``.

    Heuristics, applied in order:

    1. If the literal is inside an unclosed ``IN ( … )`` paren whose body
       is a comma list of literals (no nested SELECT) → ``in_list_member``.
    2. Else if a CASE WHEN encloses the literal (most recent ``WHEN`` with
       no intervening ``END`` between the WHEN and the literal), look at
       the subsequent ``THEN`` branch:
         - small integer flag → ``case_when_target``
         - amount column reference → ``case_when_source``
         - other → ``case_when_source`` (when uncertain, prefer source so
           Phase 7 retrieval still surfaces the function as a computer
           candidate)
    3. Default → ``filter``.

    Returns one of: ``filter``, ``case_when_source``, ``case_when_target``,
    ``in_list_member``.
    """
    # --- 1. IN-list detection ---
    open_paren_idx = _find_enclosing_open_paren(text, lit_start)
    if open_paren_idx is not None:
        # Check that "IN" precedes the open-paren (allowing whitespace).
        preceding = text[max(0, open_paren_idx - 8):open_paren_idx]
        if _IN_TAIL_RE.search(preceding):
            # And that the paren body is a literal list, NOT a subquery.
            inner = text[open_paren_idx + 1:lit_start]
            if not _SELECT_RE.search(inner):
                return "in_list_member"

    # --- 2. CASE WHEN context ---
    before_window_start = max(0, lit_start - _CASE_WINDOW_BEFORE)
    before_window = text[before_window_start:lit_start]

    # Find the LAST WHEN in the window. If an END appears between that
    # WHEN and our literal, the CASE has already closed and we're outside.
    last_when_match: re.Match[str] | None = None
    for m in _WHEN_RE.finditer(before_window):
        last_when_match = m

    if last_when_match is not None:
        between_when_and_lit = before_window[last_when_match.end():]
        if not _END_RE.search(between_when_and_lit):
            # We're inside the WHEN ... region. Look for the matching THEN.
            after_window = text[
                lit_end:lit_end + _THEN_WINDOW_AFTER
            ]
            then_match = _THEN_BRANCH_RE.search(after_window)
            if then_match:
                then_expr = then_match.group(1).strip()
                # Strip outer parens / trailing punctuation to inspect the
                # core expression.
                then_clean = then_expr.strip()
                while then_clean.startswith("(") and then_clean.endswith(")"):
                    inner = then_clean[1:-1].strip()
                    if not inner:
                        break
                    then_clean = inner
                # Small integer flag → target
                if _INT_FLAG_RE.match(then_clean):
                    return "case_when_target"
                # Amount-like column → source
                if _AMOUNT_TOKEN_RE.search(then_expr):
                    return "case_when_source"
                # Default for CASE WHEN context: source. Phase 6/7 will
                # refine; for now we lean to source so the computer
                # function still surfaces in retrieval.
                return "case_when_source"

    # --- 3. Default ---
    return "filter"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

# Each extracted record is shaped:
#   {
#     "identifier": "CAP943",
#     "function":   "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION",
#     "line":       24,            # 1-based line number
#     "role":       "case_when_target",
#   }
#
# The schema is NOT included on each record — it's the partition key in
# Redis (one literal index per schema), so storing it per record would
# duplicate the same string thousands of times.


def extract_literals(
    source_lines: list[str],
    function_name: str,
    patterns: Iterable[CompiledPattern],
) -> list[dict[str, Any]]:
    """Extract business identifier literals from *source_lines*.

    Walks each line, finds every match for every pattern (anchored
    inside ``'...'`` string literals), and classifies the role of each
    occurrence by examining the surrounding SQL text.

    Parameters
    ----------
    source_lines:
        Raw function source as a list of strings (one per line). This
        SHOULD already have comments stripped — comment-stripping is the
        loader's responsibility, not this module's. See
        :func:`src.parsing.parser.clean_source_lines`.
    function_name:
        Canonical (uppercased, underscore-normalized) function name
        recorded on each extracted record.
    patterns:
        Iterable of :class:`CompiledPattern`. Empty/exhausted iterable
        produces no extractions and returns an empty list.

    Returns
    -------
    list of dicts (see module docstring for shape). Sorted by
    ``(function, line, identifier)`` so test fixtures are deterministic.
    """
    pattern_list = list(patterns)
    if not pattern_list:
        return []

    # Build full text (with line breaks preserved) for paren-aware
    # role classification, plus per-line offset map so we can recover the
    # 1-based line number from a global index.
    full_text = "\n".join(source_lines)

    # Per-line start offsets in full_text. line_offsets[i] is the index
    # in full_text where line i (0-based) begins. Adding 1 to the
    # 0-based line number yields the 1-based line number.
    line_offsets: list[int] = [0]
    running = 0
    for line in source_lines[:-1]:
        running += len(line) + 1  # +1 for the inserted "\n"
        line_offsets.append(running)

    def _line_number_for(global_idx: int) -> int:
        """Binary-search line_offsets for the line containing *global_idx*."""
        # Linear scan is fine — OFSAA functions are typically <100 lines.
        line_idx = 0
        for li, off in enumerate(line_offsets):
            if off <= global_idx:
                line_idx = li
            else:
                break
        return line_idx + 1  # 1-based

    records: list[dict[str, Any]] = []

    # De-dup identical records (same identifier + line + role within a
    # single function). Multiple patterns can match the same token (e.g.
    # if a future pattern is a strict subset of another) — the index
    # should not list the same literal twice.
    seen: set[tuple[str, int, str]] = set()

    for pat in pattern_list:
        for m in pat.quoted.finditer(full_text):
            # group 1 is the identifier text WITHOUT the surrounding quotes
            identifier = m.group(1)
            # Position of the identifier text (skip past the opening quote)
            ident_start = m.start(1)
            ident_end = m.end(1)

            role = classify_role(full_text, ident_start, ident_end)
            line_no = _line_number_for(ident_start)

            key = (identifier, line_no, role)
            if key in seen:
                continue
            seen.add(key)

            records.append({
                "identifier": identifier,
                "function": function_name,
                "line": line_no,
                "role": role,
            })

    # Deterministic ordering for test stability.
    records.sort(key=lambda r: (r["function"], r["line"], r["identifier"]))
    return records


# ---------------------------------------------------------------------------
# Per-schema index aggregation
# ---------------------------------------------------------------------------

def merge_into_index(
    index: dict[str, list[dict[str, Any]]],
    records: list[dict[str, Any]],
) -> None:
    """Merge extracted *records* into a per-identifier *index* in place.

    *index* has the shape ``{identifier: [{function, line, role}, ...]}``.
    Records are appended to the matching identifier's list. Per-identifier
    lists are kept sorted by ``(function, line)`` so the persisted Redis
    payload is byte-stable across reloads (modulo new functions arriving).
    """
    for rec in records:
        ident = rec["identifier"]
        bucket = index.setdefault(ident, [])
        bucket.append({
            "function": rec["function"],
            "line": rec["line"],
            "role": rec["role"],
        })

    # Re-sort each touched bucket. Cheap because per-identifier lists are
    # short (single-digit on a typical OFSAA build).
    for ident, bucket in index.items():
        bucket.sort(key=lambda r: (r["function"], r["line"], r["role"]))
