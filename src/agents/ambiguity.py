"""
Identifier-ambiguity detection for RTIE.

When a user query names a target column (e.g. ``V_PROD_CODE``) that
exists in more than one table, and supplies a bare identifier (e.g.
``601013101-8604``) without naming which column the identifier belongs
to, RTIE cannot reliably pick a table. Previously the classifier would
guess — which led to silently wrong answers. This module detects the
ambiguity and produces a response that explains it and hands back
concrete rephrasings the user can copy.

The detection is a pure catalog lookup — no LLM call.
"""

from __future__ import annotations

from typing import Any, Optional

from src.middleware.correlation_id import get_correlation_id


IDENTIFIER_AMBIGUOUS_TYPE = "identifier_ambiguous"


# Classifier filter keys that look like row-identifying values (ordered
# by how likely they are to be the "thing the user is asking about").
IDENTIFIER_FILTERS: tuple[tuple[str, str], ...] = (
    ("account_number", "V_ACCOUNT_NUMBER"),
    ("gl_code",        "V_GL_CODE"),
    ("lv_code",        "V_LV_CODE"),
    ("lob_code",       "V_LOB_CODE"),
    ("branch_code",    "V_BRANCH_CODE"),
)


# Natural-language word to prefix the identifier with in user-facing
# suggestions. For GL codes the technical column name reads more
# faithfully (that's what unblocked the query in manual testing); for
# accounts/branches a short English word is friendlier.
_NATURAL_WORD_BY_COLUMN: dict[str, str] = {
    "V_ACCOUNT_NUMBER": "account",
    "V_GL_CODE":        "v_gl_code",
    "V_LV_CODE":        "v_lv_code",
    "V_LOB_CODE":       "v_lob_code",
    "V_BRANCH_CODE":    "branch",
}


# Human-readable labels used in the candidate_tables payload.
_LABEL_BY_COLUMN: dict[str, str] = {
    "V_ACCOUNT_NUMBER": "account number",
    "V_GL_CODE":        "GL code",
    "V_LV_CODE":        "LV code",
    "V_LOB_CODE":       "LOB code",
    "V_BRANCH_CODE":    "branch code",
}


# Filter columns that read better with a connecting preposition
# ("for account X") rather than being slotted directly before the
# identifier ("of account X").
_PREPOSITION_CONNECTED = frozenset({"V_ACCOUNT_NUMBER", "V_BRANCH_CODE"})


def natural_word(filter_column: str) -> str:
    """Return the user-facing disambiguator for *filter_column*.

    Falls back to the lowercased column name when no natural word is
    defined for that column.
    """
    return _NATURAL_WORD_BY_COLUMN.get(
        filter_column.upper(), filter_column.lower()
    )


def _label_for(filter_column: str) -> str:
    return _LABEL_BY_COLUMN.get(filter_column.upper(), filter_column)


def _populated_identifier(
    filters: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(filter_key, value)`` for the first populated identifier
    filter, or ``(None, None)`` when none is set."""
    for key, _col in IDENTIFIER_FILTERS:
        val = filters.get(key)
        if val not in (None, ""):
            return key, str(val)
    return None, None


def _query_already_disambiguates(
    user_query: str, candidates: list[dict]
) -> bool:
    """True if the user's text explicitly names one of the candidates'
    filter columns or natural-word forms.

    Column names (with underscores) are matched as substrings because
    they can't form substrings of other English words. Natural words
    like "account" are matched with space-padding so they don't fire
    on "accountancy" or similar.
    """
    if not user_query:
        return False
    lowered = user_query.lower()
    padded = f" {lowered} "
    for candidate in candidates:
        col = candidate["filter_column"].lower()
        if col in lowered:
            return True
        word = candidate["natural_word"].lower()
        if word == col:
            # Already matched via the column-name substring check above.
            continue
        if f" {word} " in padded or f" {word}s " in padded:
            return True
    return False


def detect_identifier_ambiguity(
    target_column: Optional[str],
    filters: dict[str, Any],
    tables_to_columns: dict[str, set[str]],
    user_query: str,
) -> Optional[list[dict[str, str]]]:
    """Return candidate tables when ``target_column`` is ambiguous, else None.

    Triggers when:
      * ``target_column`` is present in two or more tables, AND
      * an identifier filter (``account_number``, ``gl_code``, ...) is
        populated, AND
      * the candidates use different filter columns (i.e. the identifier
        would resolve differently depending on the table chosen), AND
      * the user query does not explicitly name a disambiguating column
        or natural-word form.

    Returns a list of candidate dicts with keys ``table``,
    ``filter_column``, ``label``, ``natural_word`` — or ``None`` when no
    actionable ambiguity exists.
    """
    if not target_column or not tables_to_columns:
        return None
    target = target_column.strip().upper()
    if not target:
        return None

    candidate_tables = sorted(
        t for t, cols in tables_to_columns.items() if target in cols
    )
    if len(candidate_tables) < 2:
        return None

    _filter_key, identifier_value = _populated_identifier(filters)
    if identifier_value is None:
        return None

    candidates: list[dict[str, str]] = []
    for table in candidate_tables:
        table_cols = tables_to_columns.get(table, set())
        filter_column: Optional[str] = None
        for _key, col in IDENTIFIER_FILTERS:
            if col in table_cols:
                filter_column = col
                break
        if not filter_column:
            continue
        candidates.append({
            "table": table,
            "filter_column": filter_column,
            "label": _label_for(filter_column),
            "natural_word": natural_word(filter_column),
        })

    if len(candidates) < 2:
        return None

    if len({c["filter_column"] for c in candidates}) < 2:
        return None

    if _query_already_disambiguates(user_query, candidates):
        return None

    return candidates


def _inject_disambiguator(
    user_query: str,
    identifier: str,
    filter_column: str,
    word: str,
) -> str:
    """Return *user_query* rewritten to prefix *identifier* with *word*.

    For account-style disambiguators, the surrounding preposition is
    rewritten too (``of X`` → ``for account X``) because "of account X"
    reads awkwardly. For column-style disambiguators (``v_gl_code``)
    the word is simply slotted in front of the identifier.
    """
    if not user_query:
        return f"{word} {identifier}".strip()

    if identifier not in user_query:
        return f"{user_query} ({word} {identifier})"

    if filter_column.upper() in _PREPOSITION_CONNECTED:
        for connector in (" of ", " for "):
            needle = f"{connector}{identifier}"
            if needle in user_query:
                return user_query.replace(
                    needle, f" for {word} {identifier}", 1
                )
        return user_query.replace(identifier, f"{word} {identifier}", 1)

    return user_query.replace(identifier, f"{word} {identifier}", 1)


def generate_suggestions(
    user_query: str,
    identifier: str,
    candidates: list[dict[str, str]],
) -> list[str]:
    """Return one rephrased suggestion per candidate table."""
    return [
        _inject_disambiguator(
            user_query=user_query,
            identifier=identifier,
            filter_column=c["filter_column"],
            word=c["natural_word"],
        )
        for c in candidates
    ]


def render_message(
    target_column: str,
    identifier: str,
    candidates: list[dict[str, str]],
    suggestions: list[str],
) -> str:
    """Build the user-facing message body."""
    lines = [
        f"I couldn't tell which table to query for {target_column} because "
        f"{identifier} could be either:",
    ]
    for candidate in candidates:
        lines.append(
            f"  - A {candidate['filter_column']} in {candidate['table']}"
        )
    lines.append("")
    lines.append("Try rephrasing:")
    for suggestion in suggestions:
        lines.append(f'  "{suggestion}"')
    return "\n".join(lines)


def build_identifier_ambiguous_response(
    target_column: str,
    filters: dict[str, Any],
    user_query: str,
    candidates: list[dict[str, str]],
) -> dict:
    """Construct the full ``identifier_ambiguous`` response dict.

    Shape:
        {
          "status": "identifier_ambiguous",
          "type":   "identifier_ambiguous",
          "target_column": "V_PROD_CODE",
          "identifier":    "601013101-8604",
          "candidate_tables": [{table, filter_column, label}, ...],
          "message":     "<rendered message>",
          "suggestions": ["<rephrased query>", ...],
          "correlation_id": "<id>",
        }
    """
    _filter_key, identifier = _populated_identifier(filters)
    identifier = identifier or ""
    suggestions = generate_suggestions(user_query, identifier, candidates)
    message = render_message(
        target_column=target_column,
        identifier=identifier,
        candidates=candidates,
        suggestions=suggestions,
    )
    candidate_payload = [
        {
            "table": c["table"],
            "filter_column": c["filter_column"],
            "label": c["label"],
        }
        for c in candidates
    ]
    return {
        "status": IDENTIFIER_AMBIGUOUS_TYPE,
        "type":   IDENTIFIER_AMBIGUOUS_TYPE,
        "target_column": target_column,
        "identifier": identifier,
        "candidate_tables": candidate_payload,
        "message": message,
        "suggestions": suggestions,
        "correlation_id": get_correlation_id(),
    }
