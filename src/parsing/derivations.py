"""
Derivation extraction (W35 Phase 6).

Detects the OFSAA "computer" template — a single ``MERGE INTO ... USING
(SELECT ...)`` block whose SELECT list defines:

  - one ``COND_<id>_<n>`` alias built from a ``MIN(CASE WHEN ... = '<C>'
    THEN <flag> ... END)`` whose WHEN branches name target literals;
  - one or more ``EXP_<id>_<n>`` aliases whose expressions are either a
    bare ``MAX(CASE WHEN ... = '<A>' THEN <amount_col> ... END)`` (Pattern B
    — DIRECT_ASSIGN) or a balanced subtraction
    ``MAX(CASE WHEN ... = '<A>' ...) - MAX(CASE WHEN ... = '<B>' ...)``
    (Pattern A — SUBTRACT);
  - a ``WHEN MATCHED THEN UPDATE SET <col> = CASE WHEN COND_<id>_<n>=<flag>
    THEN EXP_<id>_<n> ELSE ... END`` clause that ties each EXP back to a
    flag.

Cross-referencing those three layers yields a structured derivation record
(target literal, target column, source literals, operation, operands)
that Phase 7 will surface in CAP-code responses.

The corpus survey (Step 1) found, in ABL_CAR_CSTM_V4 (372 functions):
  - 19 functions use a clean SUBTRACT pair (Pattern A).
  - 23 use a single MAX-CASE-WHEN with a CAP literal + a COND alias
    (Pattern B candidates; only the cleanly-shaped ones are extracted).
  - 16 use shapes wrapped in GREATEST/LEAST/COALESCE/SUM or division
    (deferred — flagged as observed-but-deferred).

Pattern A and Pattern B together cover the unambiguous cases. More
exotic shapes (e.g. nested GREATEST/LEAST in CS_Available_Buffer_*)
are deliberately skipped: the target literal is still discoverable
via the Phase 5 literal index, but no derivation record is emitted.

This module does NOT mutate state — see ``loader.py`` for the
integration point that persists records on the function graph and
cross-references them from the literal index.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from src.logger import get_logger
from src.parsing.literals import CompiledPattern

logger = get_logger(__name__, concern="app")


# Operation kinds. Values are intentionally short, ALL-CAPS strings —
# Phase 7 prompts and frontend formatters will use them as enum-like keys.
OP_SUBTRACT = "SUBTRACT"
OP_DIRECT_ASSIGN = "DIRECT_ASSIGN"


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# OFSAA generated alias names. The numeric body identifies the MERGE block;
# the trailing _<n> distinguishes EXP variants (10, 11, 12, 13 …) and the
# COND that drives them.
_EXP_ALIAS_RE = re.compile(r"\bEXP_(\d+)_(\d+)\b", re.IGNORECASE)
_COND_ALIAS_RE = re.compile(r"\bCOND_(\d+)_(\d+)\b", re.IGNORECASE)

# Top-level MERGE INTO recognition. We capture the target table so the
# derivation record records what physical row was written to (independent
# of the per-derivation target_column extracted from the UPDATE SET clause).
_MERGE_INTO_RE = re.compile(
    r"\bMERGE\s+INTO\s+(\w+)\s+\w+\s+USING\s*\(",
    re.IGNORECASE,
)

# WHEN MATCHED THEN UPDATE SET — the routing layer. Group 1 is the SET
# body up to the next semicolon (we cap at the first unbalanced ``;`` in
# a balanced-paren walk; this regex just locates the start).
_WHEN_MATCHED_RE = re.compile(
    r"\bWHEN\s+MATCHED\s+THEN\s+UPDATE\s+SET\b",
    re.IGNORECASE,
)

# Aliases-in-context: matches the trailing ``AS <alias>`` of a SELECT-list
# item. Used to find COND_/EXP_ aliases. We anchor on AS to avoid stray
# matches (e.g. if a CASE body mentions COND_<id>=10, we don't want to
# treat that as an alias — only the AS-anchored occurrence is).
_AS_ALIAS_RE = re.compile(
    r"\bAS\s+(EXP_\d+_\d+|COND_\d+_\d+)\b",
    re.IGNORECASE,
)

# Inside a CASE branch THEN clause, find a column reference. Allows
# ``schema.table.col``, ``alias.col``, or bare ``col``. We use this to
# capture the amount column an EXP MAX(CASE WHEN) THEN branch returns.
_COL_REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2})\b")

# A small integer flag (10, 11, 12, …) — used inside COND CASE THEN
# branches and inside the WHEN MATCHED routing CASE WHEN COND=N THEN.
_INT_FLAG_RE = re.compile(r"^-?\d{1,4}$")

# Tokens that should never be treated as amount columns (keywords that
# can match _COL_REF_RE inside a THEN branch).
_NON_COLUMN_TOKENS = frozenset({
    "CASE", "WHEN", "THEN", "ELSE", "END", "IS", "NULL", "NOT", "AND",
    "OR", "IN", "SELECT", "FROM", "WHERE", "AS", "DISTINCT", "DEFAULT",
    "BETWEEN", "LIKE",
})


# ---------------------------------------------------------------------------
# Balanced-paren scanning
# ---------------------------------------------------------------------------

def _walk_balanced(text: str, open_idx: int) -> int:
    """Given ``text[open_idx] == '('``, return the index just past the
    matching ``)``. Skips over ``'...'`` SQL string literals so a stray
    ``)`` inside a literal doesn't unbalance the depth.

    Returns ``len(text)`` if the parens are unbalanced (degenerate input).
    """
    assert text[open_idx] == "(", "expected '(' at open_idx"
    depth = 1
    i = open_idx + 1
    n = len(text)
    while i < n and depth > 0:
        c = text[i]
        if c == "'":
            # Skip the SQL string literal. Doubled '' is the SQL escape;
            # treat it as two separate (empty) literals.
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return i


def _split_top_level(body: str, delim: str = ",") -> list[str]:
    """Split *body* at top-level *delim* characters, respecting parens
    and SQL string literals. Returns the list of pieces (each retains its
    surrounding whitespace; callers should ``.strip()`` as needed).
    """
    out: list[str] = []
    depth = 0
    start = 0
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == "'":
            j = i + 1
            while j < n:
                if body[j] == "'":
                    if j + 1 < n and body[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            if depth > 0:
                depth -= 1
        elif depth == 0 and c == delim:
            out.append(body[start:i])
            start = i + 1
        i += 1
    out.append(body[start:])
    return out


def _peel_parens(expr: str) -> str:
    """Strip a single outer pair of matched parens (with whitespace) from
    *expr* repeatedly while the parens are balanced and span the entire
    expression. Returns the inner text. Idempotent on already-bare input.
    """
    s = expr.strip()
    while s.startswith("(") and s.endswith(")"):
        # Confirm the outer ``(`` matches the very last ``)``.
        end = _walk_balanced(s, 0)
        if end == len(s):
            inner = s[1:-1].strip()
            if not inner:
                return s
            s = inner
            continue
        break
    return s


# ---------------------------------------------------------------------------
# MAX(CASE WHEN ...) operand parsing
# ---------------------------------------------------------------------------

def _find_max_call(text: str, start: int = 0) -> tuple[int, int, str] | None:
    """Find the next ``MAX(...)`` call in *text* starting at *start*.

    Returns ``(call_start, call_end, body)`` on success — *call_start* is
    the index of ``M`` in ``MAX``, *call_end* is just past the matching
    close-paren, and *body* is the inside-paren contents. Returns ``None``
    if no ``MAX(`` appears at or after *start*.
    """
    m = re.search(r"\bMAX\s*\(", text[start:], re.IGNORECASE)
    if not m:
        return None
    call_start = start + m.start()
    open_paren = start + m.end() - 1
    close_paren_after = _walk_balanced(text, open_paren)
    if close_paren_after >= len(text):
        # Unbalanced — degenerate; treat as not found.
        return None
    body = text[open_paren + 1:close_paren_after - 1]
    return (call_start, close_paren_after, body)


def _extract_operand_from_max_case(
    body: str,
    literal_patterns: Iterable[CompiledPattern],
) -> dict[str, str] | None:
    """Parse a ``MAX(CASE WHEN ... = '<L>' ... THEN <amount_col> ELSE ... END)``
    body (the *inside* of the MAX paren) and return
    ``{"literal": L, "amount_column": col}``.

    Returns ``None`` if:
      - the body doesn't start with a CASE WHEN (after ``COALESCE(...)``-
        style wrappers — we deliberately don't peel those, so a wrapped
        MAX doesn't classify cleanly);
      - no business-identifier literal appears in the WHEN clause;
      - the THEN branch doesn't yield a recognizable column reference.
    """
    body_stripped = body.strip()
    if not re.match(r"\bCASE\s+WHEN\b", body_stripped, re.IGNORECASE):
        return None

    # Find the first business-identifier literal anywhere in the body.
    # In OFSAA this is the WHEN-clause RHS (often inside a nested IN
    # (SELECT ... WHERE = '<L>') sub-pattern), and it is the only such
    # literal in the body — the THEN/ELSE branches don't contain CAP-codes.
    matched_literal: str | None = None
    for pat in literal_patterns:
        m = pat.quoted.search(body_stripped)
        if m:
            matched_literal = m.group(1)
            break
    if matched_literal is None:
        return None

    # Extract the THEN branch text. Use the same regex Phase 5 does to
    # locate the THEN-branch up to the next ELSE / WHEN / END boundary.
    then_match = re.search(
        r"\bTHEN\b\s+(.+?)(?=\s+\bELSE\b|\s+\bWHEN\b|\s+\bEND\b)",
        body_stripped,
        re.IGNORECASE | re.DOTALL,
    )
    if not then_match:
        return None
    then_expr = then_match.group(1).strip()
    # Look for a column reference. Skip CASE/WHEN/THEN/ELSE/END keywords.
    # The first non-keyword identifier wins — for OFSAA's template this is
    # always the amount column.
    amount_col: str | None = None
    for ref in _COL_REF_RE.finditer(then_expr):
        candidate = ref.group(1)
        head = candidate.split(".")[-1].upper()
        if head in _NON_COLUMN_TOKENS:
            continue
        # Skip pure numeric tokens — _COL_REF_RE doesn't match them, but
        # be defensive.
        if candidate.replace("_", "").isdigit():
            continue
        amount_col = candidate
        break
    if amount_col is None:
        return None

    return {"literal": matched_literal, "amount_column": amount_col}


def _classify_exp_expression(
    expr: str,
    literal_patterns: Iterable[CompiledPattern],
) -> dict[str, Any] | None:
    """Classify the right-hand-side expression bound to an EXP_<id>_<n>
    alias.

    Returns one of:
      - ``{"operation": "SUBTRACT", "operands": [{...}, {...}]}``
      - ``{"operation": "DIRECT_ASSIGN", "operands": [{...}]}``
      - ``None`` (expression doesn't match either supported pattern).

    The peeling strategy is intentionally narrow: we accept the canonical
    OFSAA shapes and reject everything else. Wrapping a clean MAX(CASE
    WHEN) in COALESCE / GREATEST / LEAST / SUM is treated as "not Pattern
    A or B" — Phase 7 surfacing must be high-confidence, so we'd rather
    skip than mis-classify.
    """
    pat_list = list(literal_patterns)
    if not pat_list:
        return None

    bare = _peel_parens(expr)

    # --- Pattern A: split at top-level minus sign -----------------------
    minus_parts = _split_top_level(bare, "-")
    if len(minus_parts) == 2:
        left = _peel_parens(minus_parts[0])
        right = _peel_parens(minus_parts[1])
        # Both sides must be `MAX(CASE WHEN ...)` — strict shape check.
        left_op = _try_max_case_operand(left, pat_list)
        right_op = _try_max_case_operand(right, pat_list)
        if left_op is not None and right_op is not None:
            return {
                "operation": OP_SUBTRACT,
                "operands": [left_op, right_op],
            }

    # --- Pattern B: bare MAX(CASE WHEN ...) -----------------------------
    direct = _try_max_case_operand(bare, pat_list)
    if direct is not None:
        return {
            "operation": OP_DIRECT_ASSIGN,
            "operands": [direct],
        }

    return None


def _try_max_case_operand(
    expr: str,
    literal_patterns: Iterable[CompiledPattern],
) -> dict[str, str] | None:
    """If *expr* (already paren-peeled) is a single ``MAX(CASE WHEN …)``
    call and the body parses cleanly, return the operand record.

    The whole expression must be the MAX call — leading/trailing tokens
    (e.g. ``COALESCE(MAX(...), 0)``) cause this to return ``None``.
    """
    s = expr.strip()
    m = re.match(r"\AMAX\s*\(", s, re.IGNORECASE)
    if not m:
        return None
    open_paren = m.end() - 1
    close_after = _walk_balanced(s, open_paren)
    # The MAX call must consume the entire expression — anything trailing
    # (a comma in a multi-arg COALESCE, an additional operator, …) means
    # this is a wrapped/composed shape, not a clean Pattern A/B operand.
    if close_after != len(s):
        return None
    body = s[open_paren + 1:close_after - 1]
    return _extract_operand_from_max_case(body, literal_patterns)


# ---------------------------------------------------------------------------
# COND alias parsing
# ---------------------------------------------------------------------------

def _parse_cond_expression(
    expr: str,
    literal_patterns: Iterable[CompiledPattern],
) -> dict[int, str]:
    """Parse a COND_<id>_<n> alias's expression and return ``{flag: literal}``.

    Expected shape: ``MIN(CASE WHEN <pred1> THEN <flag1> WHEN <pred2>
    THEN <flag2> ... ELSE <default_flag> END)`` — the predicates are the
    discriminator that picks which arm of the row this is.

    The ELSE branch is intentionally not recorded as a target: it
    represents the "no business-identifier matched" fallback, which in
    OFSAA's template is the no-op DIRECT_ASSIGN of the row's existing
    amount (see ``MIN(...N_STD_ACCT_HEAD_AMT) AS EXP_<id>_<n>`` in the
    fixture). Phase 7 only cares about the matched flags.

    Tolerates ``MIN(...)`` and bare ``CASE WHEN ...`` shapes — some
    functions wrap the CASE in MIN/MAX, others don't.
    """
    pat_list = list(literal_patterns)
    if not pat_list:
        return {}
    s = expr.strip()
    # Peel a single leading function call wrapper if present (MIN/MAX).
    leading_call = re.match(r"\b(MIN|MAX)\s*\(", s, re.IGNORECASE)
    if leading_call:
        open_paren = leading_call.end() - 1
        close_after = _walk_balanced(s, open_paren)
        if close_after == len(s):
            s = s[open_paren + 1:close_after - 1].strip()
    # Now s should start with CASE WHEN.
    if not re.match(r"\bCASE\b", s, re.IGNORECASE):
        return {}

    # Find each "WHEN ... THEN <flag>" pair. We scan for WHEN tokens and
    # match each one against the next THEN; the WHEN body is the slice
    # between them. We use top-level scanning: nested CASE inside a WHEN
    # body would derail this, but the OFSAA COND template never nests.
    flag_to_lit: dict[int, str] = {}
    when_positions = [m.start() for m in re.finditer(r"\bWHEN\b", s, re.IGNORECASE)]
    then_positions = [m.start() for m in re.finditer(r"\bTHEN\b", s, re.IGNORECASE)]
    end_match = re.search(r"\bEND\b", s, re.IGNORECASE)

    if not when_positions or not then_positions:
        return {}

    # Pair the i-th WHEN with the i-th THEN. The shared length defines
    # how many WHEN/THEN pairs to emit — robust to a missing ELSE.
    n_pairs = min(len(when_positions), len(then_positions))
    for i in range(n_pairs):
        when_pos = when_positions[i]
        then_pos = then_positions[i]
        if then_pos <= when_pos:
            continue
        # Bound the THEN body by the next WHEN (or the trailing END/ELSE).
        next_when = when_positions[i + 1] if i + 1 < n_pairs else None
        else_match = re.search(r"\bELSE\b", s[then_pos:], re.IGNORECASE)
        end_after = (
            then_pos + else_match.start() if else_match
            else end_match.start() if end_match
            else len(s)
        )
        bound = end_after
        if next_when is not None and next_when < bound:
            bound = next_when

        when_body = s[when_pos + len("WHEN"):then_pos]
        then_body = s[then_pos + len("THEN"):bound].strip()

        # Extract the first literal that matches any pattern from the
        # WHEN body — that's the target literal for this flag.
        target_lit: str | None = None
        for pat in pat_list:
            lm = pat.quoted.search(when_body)
            if lm:
                target_lit = lm.group(1)
                break
        if target_lit is None:
            continue

        # Extract the integer flag from the THEN body. Strip outer parens
        # and trailing punctuation for shape-resilience.
        flag_text = _peel_parens(then_body).strip().rstrip(",;")
        if _INT_FLAG_RE.match(flag_text):
            flag_to_lit[int(flag_text)] = target_lit

    return flag_to_lit


# ---------------------------------------------------------------------------
# WHEN MATCHED routing parser
# ---------------------------------------------------------------------------

def _parse_when_matched_routing(
    routing_text: str,
) -> list[dict[str, Any]]:
    """Parse the body of ``WHEN MATCHED THEN UPDATE SET ...`` and return
    a list of ``{column, branches: [{flag, exp_alias}, ...]}`` records.

    Expected per-column shape (multi-target functions like CS_Available_
    Buffer_from_CET1_Capital have several ``WHEN COND=flag THEN EXP_n``
    branches; the simplest case has one):

        TT.<col> = CASE WHEN <COND_alias>=<flag1> THEN <EXP_alias1>
                       [WHEN <COND_alias>=<flag2> THEN <EXP_alias2>]
                       ELSE <fallback> END

    Fallback expressions (typically a no-op EXP_<n> aliased to MIN(target_col))
    are deliberately ignored — they don't correspond to a derivation
    target literal.
    """
    # Split at top-level commas to handle multi-column UPDATE SET clauses.
    parts = _split_top_level(routing_text, ",")
    out: list[dict[str, Any]] = []
    for part in parts:
        eq_split = _split_top_level(part, "=")
        if len(eq_split) < 2:
            continue
        col_text = eq_split[0].strip()
        # Drop the table alias prefix if any: TT.N_STD_ACCT_HEAD_AMT -> N_STD_ACCT_HEAD_AMT.
        if "." in col_text:
            col_text = col_text.split(".")[-1]
        col_text = col_text.strip()
        if not col_text:
            continue

        # The RHS is everything after the first '='. Re-join, then peel.
        rhs = "=".join(eq_split[1:]).strip()

        branches: list[dict[str, Any]] = []
        # Look for "WHEN COND_<id>_<n>=<flag> THEN EXP_<id>_<n>" patterns.
        for m in re.finditer(
            r"\bWHEN\s+(COND_\d+_\d+)\s*=\s*(-?\d{1,4})\s+THEN\s+(EXP_\d+_\d+)\b",
            rhs,
            re.IGNORECASE,
        ):
            branches.append({
                "cond_alias": m.group(1).upper(),
                "flag": int(m.group(2)),
                "exp_alias": m.group(3).upper(),
            })
        if branches:
            out.append({"column": col_text.upper(), "branches": branches})
    return out


# ---------------------------------------------------------------------------
# MERGE block extraction
# ---------------------------------------------------------------------------

def _find_merge_blocks(text: str) -> list[dict[str, Any]]:
    """Find every ``MERGE INTO X TT USING (SELECT ... ) SS ...`` block.

    Returns a list of dicts with:
      - ``target_table``: name from MERGE INTO <table> <alias>
      - ``select_body``: text inside the USING (...) parens
      - ``post_select``: text from just after the USING(...) close-paren
        through to the trailing semicolon (used to find ON / WHEN MATCHED)
      - ``span``: (merge_start, merge_end) — start/end indices in *text*
    """
    blocks: list[dict[str, Any]] = []
    for m in _MERGE_INTO_RE.finditer(text):
        merge_start = m.start()
        target_table = m.group(1)
        # The USING ( open-paren is at m.end() - 1.
        using_open = m.end() - 1
        using_close_after = _walk_balanced(text, using_open)
        if using_close_after >= len(text):
            continue
        select_body = text[using_open + 1:using_close_after - 1]

        # Walk forward to a semicolon at top level — the MERGE statement
        # terminator. (No nested MERGEs inside an inline expression in
        # OFSAA, so a top-level ; is the right boundary.)
        depth = 0
        i = using_close_after
        n = len(text)
        merge_end = n
        while i < n:
            c = text[i]
            if c == "'":
                j = i + 1
                while j < n:
                    if text[j] == "'":
                        if j + 1 < n and text[j + 1] == "'":
                            j += 2
                            continue
                        break
                    j += 1
                i = j + 1
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                if depth > 0:
                    depth -= 1
            elif depth == 0 and c == ";":
                merge_end = i + 1
                break
            i += 1

        post_select = text[using_close_after:merge_end]
        blocks.append({
            "target_table": target_table.upper(),
            "select_body": select_body,
            "post_select": post_select,
            "span": (merge_start, merge_end),
        })
    return blocks


# ---------------------------------------------------------------------------
# SELECT-list alias map
# ---------------------------------------------------------------------------

def _build_alias_expression_map(select_body: str) -> dict[str, str]:
    """Walk the SELECT list inside a USING(...) body and return a dict
    mapping ``{ALIAS: expression_text}`` for every COND_<id>_<n> and
    EXP_<id>_<n> alias.

    Splits at top-level commas (paren- and string-aware), then for each
    item finds ``AS <alias>`` and treats the text BEFORE that ``AS`` as
    the bound expression. Items with no AS-anchored alias of interest
    are skipped silently.
    """
    # The SELECT body might have a leading SELECT keyword and an optional
    # /*+ HINT */ block. Drop them so the first comma-split item starts
    # with a real expression.
    body = re.sub(r"\A\s*SELECT\b", "", select_body, count=1, flags=re.IGNORECASE)
    body = re.sub(
        r"\A\s*/\*\+.*?\*/", "", body, count=1, flags=re.DOTALL,
    )
    # OFSAA also occasionally puts the FROM keyword in the same body; we
    # truncate at the first top-level FROM so we don't accidentally treat
    # a join sub-expression as a SELECT-list alias.
    select_only = _truncate_at_top_level_keyword(body, "FROM")

    out: dict[str, str] = {}
    for raw_item in _split_top_level(select_only, ","):
        item = raw_item.strip()
        if not item:
            continue
        # Find the LAST AS-anchored alias in the item — there might be
        # nested aliases inside the expression (rare in OFSAA, but be
        # defensive). The trailing AS is what binds the whole item.
        matches = list(_AS_ALIAS_RE.finditer(item))
        if not matches:
            continue
        last = matches[-1]
        alias = last.group(1).upper()
        expr = item[:last.start()].strip()
        if expr:
            out[alias] = expr
    return out


def _truncate_at_top_level_keyword(text: str, keyword: str) -> str:
    """Return *text* truncated at the first top-level occurrence of
    ``\\b<keyword>\\b`` (case-insensitive). Top-level = depth 0 paren and
    not inside a string literal. Returns the entire *text* if no such
    occurrence exists.
    """
    pat = re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE)
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        if depth == 0:
            m = pat.match(text, i)
            if m:
                return text[:m.start()]
        i += 1
    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_derivations(
    source_lines: list[str],
    function_name: str,
    patterns: Iterable[CompiledPattern],
) -> list[dict[str, Any]]:
    """Extract structured derivation records from *source_lines*.

    Detects the OFSAA computer template and emits one record per
    (target_literal, target_column) pair. Each record has the shape:

        {
            "target_literal":  "CAP943",
            "target_column":   "N_STD_ACCT_HEAD_AMT",
            "source_literals": ["CAP309", "CAP863"],
            "operation":       "SUBTRACT",
            "operands": [
                {"literal": "CAP309", "amount_column": "..."},
                {"literal": "CAP863", "amount_column": "..."},
            ],
            "function":        "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION",
            "line_range":      [24, 24],
        }

    Parameters mirror :func:`src.parsing.literals.extract_literals`.
    Returns an empty list when no derivation pattern is detected — this is
    the expected outcome for loader/router functions that contain CAP-codes
    purely as filters or IN-list members.

    Pure function: no Redis writes, no logging side effects.
    """
    pat_list = list(patterns)
    if not pat_list:
        return []

    full_text = "\n".join(source_lines)
    if not _MERGE_INTO_RE.search(full_text):
        return []

    # Build a per-line offset map for line_range translation.
    line_offsets: list[int] = [0]
    running = 0
    for line in source_lines[:-1]:
        running += len(line) + 1  # +1 for the inserted "\n"
        line_offsets.append(running)

    def line_for_offset(idx: int) -> int:
        n = len(line_offsets)
        # Linear scan is fine — typical OFSAA function is <100 lines.
        line_idx = 0
        for li, off in enumerate(line_offsets):
            if off <= idx:
                line_idx = li
            else:
                break
        return line_idx + 1  # 1-based

    out: list[dict[str, Any]] = []

    for block in _find_merge_blocks(full_text):
        select_body = block["select_body"]
        post_select = block["post_select"]
        target_table = block["target_table"]
        merge_start, merge_end = block["span"]
        line_start = line_for_offset(merge_start)
        line_end = line_for_offset(max(merge_end - 1, merge_start))

        # --- 1. Build alias -> expression map ---------------------------
        alias_exprs = _build_alias_expression_map(select_body)
        if not alias_exprs:
            continue

        # --- 2. Parse COND aliases into {flag -> literal} maps ---------
        cond_maps: dict[str, dict[int, str]] = {}
        for alias, expr in alias_exprs.items():
            if alias.startswith("COND_"):
                m = _parse_cond_expression(expr, pat_list)
                if m:
                    cond_maps[alias] = m

        if not cond_maps:
            continue

        # --- 3. Classify EXP aliases -----------------------------------
        exp_classes: dict[str, dict[str, Any]] = {}
        for alias, expr in alias_exprs.items():
            if alias.startswith("EXP_"):
                cls = _classify_exp_expression(expr, pat_list)
                if cls is not None:
                    exp_classes[alias] = cls

        if not exp_classes:
            continue

        # --- 4. Parse WHEN MATCHED routing -----------------------------
        wm = _WHEN_MATCHED_RE.search(post_select)
        if not wm:
            continue
        # The routing text runs from after WHEN MATCHED THEN UPDATE SET
        # up to (but not including) the trailing semicolon — which is the
        # last char of post_select after _find_merge_blocks's truncation.
        routing_text = post_select[wm.end():].rstrip()
        if routing_text.endswith(";"):
            routing_text = routing_text[:-1]
        routing = _parse_when_matched_routing(routing_text)
        if not routing:
            continue

        # --- 5. Cross-reference ----------------------------------------
        # For each (col, flag, exp_alias) routing entry, look up:
        #   target_literal := COND_map[flag] (the literal that triggers
        #     this flag, drawn from the corresponding COND alias)
        #   exp_class     := exp_classes[exp_alias] (operation+operands)
        # Skip routings whose EXP alias didn't classify (no clean shape).
        seen: set[tuple[str, str]] = set()
        for col_record in routing:
            col = col_record["column"]
            for branch in col_record["branches"]:
                cond_alias = branch["cond_alias"]
                flag = branch["flag"]
                exp_alias = branch["exp_alias"]
                cond_map = cond_maps.get(cond_alias)
                if not cond_map:
                    continue
                target_lit = cond_map.get(flag)
                if target_lit is None:
                    continue
                exp_cls = exp_classes.get(exp_alias)
                if exp_cls is None:
                    continue

                key = (target_lit, col)
                if key in seen:
                    # A function shouldn't write the same (literal, column)
                    # twice in a single MERGE; if it does, keep the first.
                    continue
                seen.add(key)

                source_literals = [op["literal"] for op in exp_cls["operands"]]
                out.append({
                    "target_literal": target_lit,
                    "target_column": col,
                    "source_literals": source_literals,
                    "operation": exp_cls["operation"],
                    "operands": list(exp_cls["operands"]),
                    "function": function_name,
                    "line_range": [line_start, line_end],
                })

    # Deterministic ordering for test/redis stability.
    out.sort(key=lambda r: (r["function"], r["line_range"][0], r["target_literal"]))
    return out


# ---------------------------------------------------------------------------
# Cross-reference helper for the literal index
# ---------------------------------------------------------------------------

def attach_derivations_to_literal_index(
    literal_index: dict[str, list[dict[str, Any]]],
    derivations: list[dict[str, Any]],
) -> None:
    """Add a ``derivation`` field on each literal-index record whose
    ``(function, role)`` matches a derivation's target.

    Mutates *literal_index* in place. The added field is the embedded
    derivation summary (operation, source_literals, target_column) — NOT
    the full operands list — so the literal-index payload stays compact.

    Phase 7 routing reads ``graph:literal:<schema>:<id>``; when a
    case_when_target record carries a derivation, Phase 7 can format
    "CAP943 = CAP309 - CAP863" directly from the index without round-
    tripping to the function graph. (The full record IS still on the
    function graph for callers that need operands / line_range.)
    """
    if not derivations:
        return

    # Build a lookup: (target_literal, function) -> summary. A function
    # writing one literal twice in different MERGE blocks would collapse
    # here; the dedup in extract_derivations means that doesn't happen.
    summary_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for d in derivations:
        key = (d["target_literal"], d["function"])
        summary_by_key[key] = {
            "operation": d["operation"],
            "source_literals": list(d["source_literals"]),
            "target_column": d["target_column"],
        }

    for ident, records in literal_index.items():
        for rec in records:
            # We only attach to case_when_target rows — the role that
            # marks the literal as the OUTPUT of a computer expression.
            # filter / case_when_source / in_list_member rows are
            # untouched (the derivation isn't FOR them).
            if rec.get("role") != "case_when_target":
                continue
            summary = summary_by_key.get((ident, rec["function"]))
            if summary is None:
                continue
            rec["derivation"] = dict(summary)
