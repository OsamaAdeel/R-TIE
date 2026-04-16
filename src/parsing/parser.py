"""
PL/SQL source code parser.
Extracts raw operation blocks from source lines.
No LLM. Pure Python. Regex + structural analysis.
"""

import re
from typing import Any

# Module-level compiled regex patterns
PATTERNS = {
    "INSERT": re.compile(r'^\s*INSERT\s+INTO\s+(\w+)', re.IGNORECASE),
    "UPDATE": re.compile(r'^\s*UPDATE\s+(\w+)', re.IGNORECASE),
    "MERGE": re.compile(r'^\s*MERGE\s+INTO\s+(\w+)', re.IGNORECASE),
    "DELETE": re.compile(r'^\s*DELETE\s+FROM\s+(\w+)', re.IGNORECASE),
    "SELECT_INTO": re.compile(
        r'\bSELECT\b.+?\bINTO\s+(\w+)', re.IGNORECASE | re.DOTALL,
    ),
    "COMMIT": re.compile(r'^\s*COMMIT\s*;', re.IGNORECASE),
    "WHILE": re.compile(r'^\s*WHILE\s+(.+?)\s+LOOP', re.IGNORECASE),
    "FOR_LOOP": re.compile(r'^\s*FOR\s+\w+\s+IN\s+', re.IGNORECASE),
    "IF_MONTH": re.compile(
        r'IF\s+TO_NUMBER\s*\(\s*EXTRACT\s*\(\s*MONTH', re.IGNORECASE,
    ),
    "IF_EXTRACT": re.compile(
        r'EXTRACT\s*\(\s*(MONTH|YEAR)\s+FROM', re.IGNORECASE,
    ),
    "BLOCK_COMMENT_START": re.compile(r'/\*'),
    "BLOCK_COMMENT_END": re.compile(r'\*/'),
    "LINE_COMMENT": re.compile(r'^\s*--'),
    "FUNCTION_DEF": re.compile(
        r'CREATE\s+OR\s+REPLACE\s+FUNCTION\s+(\w+\.)?(\w+)', re.IGNORECASE,
    ),
    "FROM_TABLE": re.compile(r'\bFROM\s+(\w+)', re.IGNORECASE),
    "JOIN_TABLE": re.compile(r'\bJOIN\s+(\w+)', re.IGNORECASE),
    "USING_SUBQUERY": re.compile(r'\bUSING\s*\(', re.IGNORECASE),
    "END_LOOP": re.compile(r'^\s*END\s+LOOP\s*;', re.IGNORECASE),
    "BEGIN": re.compile(r'^\s*BEGIN\b', re.IGNORECASE),
    "END_IF": re.compile(r'^\s*END\s+IF\s*;', re.IGNORECASE),
    "SEMICOLON": re.compile(r';\s*$'),
    "NVL": re.compile(r'NVL\s*\(', re.IGNORECASE),
    "COALESCE": re.compile(r'COALESCE\s*\(', re.IGNORECASE),
    "DECODE": re.compile(r'DECODE\s*\(', re.IGNORECASE),
    "CASE": re.compile(r'\bCASE\b', re.IGNORECASE),
    "ARITHMETIC": re.compile(r'[\+\-\*\/]'),
    "WHERE": re.compile(r'\bWHERE\b', re.IGNORECASE),
    "UNION": re.compile(r'\bUNION\b', re.IGNORECASE),
    "SET_CLAUSE": re.compile(r'^\s*SET\b', re.IGNORECASE),
}

# DML keywords that start a new operation block
_DML_STARTS = ("INSERT", "UPDATE", "MERGE", "DELETE")

# Reserved words that should never be treated as table names
_RESERVED_WORDS = frozenset({
    "SELECT", "FROM", "WHERE", "SET", "INTO", "VALUES", "ON", "AND", "OR",
    "NOT", "NULL", "IS", "IN", "BETWEEN", "LIKE", "EXISTS", "CASE", "WHEN",
    "THEN", "ELSE", "END", "AS", "ALL", "DUAL", "MATCHED", "USING",
    "LOOP", "IF", "ELSIF", "BEGIN", "RETURN", "EXCEPTION", "COMMIT",
    "ROLLBACK", "DECLARE", "CURSOR", "OPEN", "FETCH", "CLOSE", "FOR",
    "WHILE", "EXIT", "PRAGMA", "SEQUENCE", "NEXTVAL", "CURRVAL",
    "SYSDATE", "SYSTIMESTAMP", "EXTRACT", "MONTH", "YEAR", "DAY",
})


def _is_table_name(name: str) -> bool:
    """Return True if *name* looks like a real table name (not a keyword)."""
    return name.upper() not in _RESERVED_WORDS


# ---------------------------------------------------------------------------
# Block-comment tracking
# ---------------------------------------------------------------------------

def _build_comment_map(lines: list[str]) -> list[bool]:
    """Build a per-line boolean list: True if the line is inside a block comment.

    Handles nested ``/* */`` only at a single level (Oracle does not support
    nested block comments).  A line that contains ``/*`` but not ``*/`` starts
    a comment region; the region continues until a line containing ``*/``.
    """
    in_comment = False
    comment_map: list[bool] = []
    for line in lines:
        if in_comment:
            comment_map.append(True)
            if PATTERNS["BLOCK_COMMENT_END"].search(line):
                in_comment = False
        else:
            if PATTERNS["BLOCK_COMMENT_START"].search(line):
                # Check whether the comment closes on the same line
                if not PATTERNS["BLOCK_COMMENT_END"].search(line):
                    in_comment = True
                # The line itself is (at least partly) a comment line, but we
                # only mark *fully-enclosed* lines.  For safety, mark it.
                comment_map.append(True)
            else:
                comment_map.append(False)
    return comment_map


def is_in_block_comment(lines: list[str], line_idx: int) -> bool:
    """Return True when *line_idx* (0-based) falls inside ``/* … */``.

    Scans from the top of *lines* to build the comment map, then returns the
    value for the requested index.
    """
    comment_map = _build_comment_map(lines)
    if 0 <= line_idx < len(comment_map):
        return comment_map[line_idx]
    return False


def _is_comment_line(line: str) -> bool:
    """True when *line* is a single-line ``--`` comment."""
    return bool(PATTERNS["LINE_COMMENT"].match(line))


# ---------------------------------------------------------------------------
# Execution-condition detection
# ---------------------------------------------------------------------------

def detect_execution_condition(lines: list[str]) -> dict | None:
    """Detect a top-level ``IF EXTRACT(MONTH …) = N`` guard.

    Scans up to 30 lines after the first ``BEGIN`` statement.  Returns a dict
    ``{"raw_condition": str, "field": "MONTH"|"YEAR", "value": str,
    "line_number": int}`` or ``None``.
    """
    begin_idx: int | None = None
    for idx, line in enumerate(lines):
        if PATTERNS["BEGIN"].match(line):
            begin_idx = idx
            break

    if begin_idx is None:
        return None

    scan_end = min(begin_idx + 31, len(lines))
    comment_map = _build_comment_map(lines)

    for idx in range(begin_idx + 1, scan_end):
        if comment_map[idx] or _is_comment_line(lines[idx]):
            continue
        stripped = lines[idx].strip()
        if not stripped:
            continue
        # Look for: IF … EXTRACT(MONTH|YEAR FROM …) = <value>
        if re.match(r'^\s*IF\b', stripped, re.IGNORECASE):
            m_extract = PATTERNS["IF_EXTRACT"].search(stripped)
            if m_extract:
                field = m_extract.group(1).upper()
                # Try to grab the comparison value (e.g., ``= 12``)
                m_val = re.search(
                    r'EXTRACT\s*\(.+?\)\s*\)?\s*=\s*(\d+)',
                    stripped,
                    re.IGNORECASE,
                )
                value = m_val.group(1) if m_val else "?"
                return {
                    "raw_condition": stripped,
                    "field": field,
                    "value": value,
                    "line_number": idx + 1,  # 1-based
                }
    return None


# ---------------------------------------------------------------------------
# Finding the end of an operation block
# ---------------------------------------------------------------------------

def find_block_end(lines: list[str], start: int, block_type: str) -> int:
    """Find the 0-based index of the last line belonging to the block.

    Parameters
    ----------
    lines:
        Full source as a list of strings.
    start:
        0-based index of the opening keyword line.
    block_type:
        One of ``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``,
        ``SELECT_INTO``, ``WHILE``, ``FOR_LOOP``.

    Returns
    -------
    int
        0-based index of the last line that belongs to the block.
    """
    total = len(lines)

    # ---- loops: find matching END LOOP ----
    if block_type in ("WHILE", "FOR_LOOP"):
        depth = 1
        idx = start + 1
        while idx < total:
            line = lines[idx]
            # Increase depth on nested LOOP openers
            if (PATTERNS["WHILE"].match(line)
                    or PATTERNS["FOR_LOOP"].match(line)):
                depth += 1
            if PATTERNS["END_LOOP"].match(line):
                depth -= 1
                if depth == 0:
                    return idx
            idx += 1
        return total - 1  # unterminated — return last line

    # ---- MERGE: balanced-parenthesis aware, ends at top-level semicolon ----
    if block_type == "MERGE":
        paren_depth = 0
        idx = start
        while idx < total:
            line = lines[idx]
            paren_depth += line.count('(') - line.count(')')
            if paren_depth <= 0 and PATTERNS["SEMICOLON"].search(line):
                return idx
            idx += 1
        return total - 1

    # ---- INSERT: ends at COMMIT, next DML start, or top-level semicolon ----
    if block_type == "INSERT":
        paren_depth = 0
        # We track parentheses so that semicolons inside sub-selects don't
        # trick us.  The INSERT … SELECT pattern normally does not have an
        # inner semicolon, but safety-first.
        idx = start
        while idx < total:
            line = lines[idx]
            paren_depth += line.count('(') - line.count(')')
            # A semicolon at top-level paren depth closes the INSERT
            if paren_depth <= 0 and PATTERNS["SEMICOLON"].search(line):
                return idx
            # COMMIT right after closes the block (COMMIT itself is separate)
            if idx > start and PATTERNS["COMMIT"].match(line):
                return idx - 1
            # Another DML starting means our INSERT ended on the previous line
            if idx > start:
                for key in _DML_STARTS:
                    if PATTERNS[key].match(line):
                        return idx - 1
            idx += 1
        return total - 1

    # ---- UPDATE / DELETE / SELECT_INTO: end at the top-level semicolon ----
    paren_depth = 0
    idx = start
    while idx < total:
        line = lines[idx]
        paren_depth += line.count('(') - line.count(')')
        if paren_depth <= 0 and PATTERNS["SEMICOLON"].search(line):
            return idx
        idx += 1
    return total - 1


# ---------------------------------------------------------------------------
# Table-name extraction
# ---------------------------------------------------------------------------

def extract_table_names(raw_lines: list[str], block_type: str) -> dict:
    """Return ``{"target_table": str|None, "source_tables": list[str]}``.

    For INSERT/UPDATE/DELETE/MERGE the *target_table* comes from the first
    keyword match.  *source_tables* are harvested from ``FROM`` and ``JOIN``
    clauses.
    """
    text = "\n".join(raw_lines)
    target_table: str | None = None
    source_tables: list[str] = []

    # -- target --
    target_pat = {
        "INSERT": PATTERNS["INSERT"],
        "UPDATE": PATTERNS["UPDATE"],
        "MERGE": PATTERNS["MERGE"],
        "DELETE": PATTERNS["DELETE"],
    }
    if block_type in target_pat:
        m = target_pat[block_type].search(text)
        if m:
            target_table = m.group(1)

    # -- source tables from FROM / JOIN --
    seen: set[str] = set()
    for m in PATTERNS["FROM_TABLE"].finditer(text):
        name = m.group(1)
        if _is_table_name(name) and name.upper() != (target_table or "").upper():
            upper = name.upper()
            if upper not in seen:
                source_tables.append(name)
                seen.add(upper)

    for m in PATTERNS["JOIN_TABLE"].finditer(text):
        name = m.group(1)
        if _is_table_name(name) and name.upper() != (target_table or "").upper():
            upper = name.upper()
            if upper not in seen:
                source_tables.append(name)
                seen.add(upper)

    return {"target_table": target_table, "source_tables": source_tables}


# ---------------------------------------------------------------------------
# Column-map extraction
# ---------------------------------------------------------------------------

def _balance_parens(text: str) -> list[str]:
    """Split *text* on commas that are not inside parentheses."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _extract_insert_columns(text: str) -> tuple[list[str], list[str]]:
    """Parse INSERT INTO tbl (col1, col2, …) SELECT val1, val2, … .

    Returns (column_names, select_expressions).
    """
    # Find the column list between the first pair of parens after INSERT INTO tbl
    col_match = re.search(
        r'INSERT\s+INTO\s+\w+\s*\(([^)]+)\)',
        text,
        re.IGNORECASE | re.DOTALL,
    )
    columns: list[str] = []
    if col_match:
        columns = [c.strip() for c in col_match.group(1).split(',')]

    # Find the SELECT clause; skip over any sub-SELECT inside the column list.
    # The main SELECT is the first SELECT that comes after the closing paren of
    # the column list.
    select_start = None
    if col_match:
        after_cols = col_match.end()
        m_sel = re.search(r'\bSELECT\b', text[after_cols:], re.IGNORECASE)
        if m_sel:
            select_start = after_cols + m_sel.end()
    else:
        m_sel = re.search(r'\bSELECT\b', text, re.IGNORECASE)
        if m_sel:
            select_start = m_sel.end()

    values: list[str] = []
    if select_start is not None:
        # Grab everything between SELECT and FROM (top-level FROM)
        rest = text[select_start:]
        # Find top-level FROM (not inside parens)
        depth = 0
        from_pos = None
        i = 0
        while i < len(rest):
            if rest[i] == '(':
                depth += 1
            elif rest[i] == ')':
                depth -= 1
            elif depth == 0 and rest[i:i+4].upper() == 'FROM':
                # Make sure it's a whole word
                before_ok = (i == 0 or not rest[i-1].isalnum())
                after_ok = (i + 4 >= len(rest) or not rest[i+4].isalnum())
                if before_ok and after_ok:
                    from_pos = i
                    break
            i += 1

        select_body = rest[:from_pos] if from_pos else rest
        # Strip trailing semicolons / whitespace
        select_body = select_body.rstrip().rstrip(';').strip()
        values = _balance_parens(select_body)

    return columns, values


def _extract_update_set(text: str) -> list[tuple[str, str]]:
    """Parse UPDATE … SET col1 = expr1, col2 = expr2 … WHERE …

    Returns a list of (column, expression) tuples.
    """
    # Locate the SET keyword
    m_set = re.search(r'\bSET\b', text, re.IGNORECASE)
    if not m_set:
        return []

    after_set = text[m_set.end():]

    # Find top-level WHERE (not inside parens)
    depth = 0
    where_pos = None
    i = 0
    while i < len(after_set):
        if after_set[i] == '(':
            depth += 1
        elif after_set[i] == ')':
            depth -= 1
        elif depth == 0 and after_set[i:i+5].upper() == 'WHERE':
            before_ok = (i == 0 or not after_set[i-1].isalnum())
            after_ok = (i + 5 >= len(after_set) or not after_set[i+5].isalnum())
            if before_ok and after_ok:
                where_pos = i
                break
        i += 1

    set_body = after_set[:where_pos] if where_pos else after_set
    set_body = set_body.rstrip().rstrip(';').strip()

    # Split on top-level commas
    assignments = _balance_parens(set_body)
    pairs: list[tuple[str, str]] = []
    for a in assignments:
        eq_idx = a.find('=')
        if eq_idx != -1:
            col = a[:eq_idx].strip()
            val = a[eq_idx + 1:].strip()
            # Strip optional table alias prefix (e.g. "t.COL")
            if '.' in col:
                col = col.split('.')[-1]
            pairs.append((col, val))
    return pairs


def extract_column_maps(raw_lines: list[str], block_type: str) -> dict:
    """Return column mappings for the block.

    For ``INSERT`` returns ``{"columns": [...], "values": [...], "mapping": {col: val}}``.
    For ``UPDATE`` returns ``{"assignments": [(col, expr), …]}``.
    For ``MERGE``  returns the UPDATE SET portion using the same UPDATE logic.
    Otherwise returns ``{}``.
    """
    text = "\n".join(raw_lines)

    if block_type == "INSERT":
        columns, values = _extract_insert_columns(text)
        mapping: dict[str, str] = {}
        for idx, col in enumerate(columns):
            if idx < len(values):
                mapping[col] = values[idx]
        return {"columns": columns, "values": values, "mapping": mapping}

    if block_type in ("UPDATE", "MERGE"):
        pairs = _extract_update_set(text)
        return {"assignments": pairs}

    return {}


# ---------------------------------------------------------------------------
# WHERE-condition extraction
# ---------------------------------------------------------------------------

def extract_conditions(raw_lines: list[str]) -> list[str]:
    """Extract WHERE clause conditions, split on top-level ``AND``.

    Returns a list of individual condition strings.  Conditions inside
    parenthesised sub-expressions are kept intact (only top-level ANDs are
    used as split points).
    """
    text = "\n".join(raw_lines)

    # Locate the *last* top-level WHERE (MERGE may have multiple)
    depth = 0
    where_pos: int | None = None
    i = 0
    while i < len(text):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
        elif depth == 0 and text[i:i+5].upper() == 'WHERE':
            before_ok = (i == 0 or not text[i-1].isalnum())
            after_ok = (i + 5 >= len(text) or not text[i+5].isalnum())
            if before_ok and after_ok:
                where_pos = i
        i += 1

    if where_pos is None:
        return []

    after_where = text[where_pos + 5:]
    # Truncate at the next top-level keyword that ends the WHERE scope
    end_keywords = ("ORDER", "GROUP", "HAVING", "UNION", "MINUS", "INTERSECT",
                    "WHEN", "RETURNING")
    depth = 0
    cut: int | None = None
    j = 0
    while j < len(after_where):
        if after_where[j] == '(':
            depth += 1
        elif after_where[j] == ')':
            depth -= 1
        elif depth == 0:
            for kw in end_keywords:
                kw_len = len(kw)
                if after_where[j:j+kw_len].upper() == kw:
                    before_ok = (j == 0 or not after_where[j-1].isalnum())
                    after_ok = (j + kw_len >= len(after_where)
                                or not after_where[j+kw_len].isalnum())
                    if before_ok and after_ok:
                        cut = j
                        break
            if cut is not None:
                break
        j += 1

    where_body = after_where[:cut] if cut else after_where
    where_body = where_body.rstrip().rstrip(';').strip()

    # Split on top-level AND
    conditions: list[str] = []
    depth = 0
    current: list[str] = []
    k = 0
    while k < len(where_body):
        ch = where_body[k]
        if ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif (depth == 0
              and where_body[k:k+3].upper() == 'AND'
              and (k == 0 or not where_body[k-1].isalnum())
              and (k + 3 >= len(where_body) or not where_body[k+3].isalnum())):
            cond = "".join(current).strip()
            if cond:
                conditions.append(cond)
            current = []
            k += 3
            continue
        else:
            current.append(ch)
        k += 1

    tail = "".join(current).strip()
    if tail:
        conditions.append(tail)

    return conditions


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _classify_line(line: str) -> str | None:
    """Return the block_type if *line* starts a recognisable operation."""
    for key in ("INSERT", "MERGE", "UPDATE", "DELETE"):
        if PATTERNS[key].match(line):
            return key
    if PATTERNS["WHILE"].match(line):
        return "WHILE"
    if PATTERNS["FOR_LOOP"].match(line):
        return "FOR_LOOP"
    # SELECT INTO — must NOT be inside a MERGE/INSERT (caller checks context)
    if re.search(r'\bSELECT\b', line, re.IGNORECASE):
        # We need multiple lines to detect INTO; handled in parse_function
        return None
    return None


def parse_function(
    source_lines: list[str],
    function_name: str,
) -> dict[str, Any]:
    """Parse a PL/SQL function body and return structured block data.

    Parameters
    ----------
    source_lines:
        The full source code as a list of strings (one per line).
    function_name:
        The name of the function being parsed (for labelling).

    Returns
    -------
    dict with keys:
        - ``function_name`` (str)
        - ``execution_condition`` (dict | None)
        - ``raw_blocks`` (list[dict])  — each dict is a *RawBlock*
        - ``total_lines`` (int)

    A *RawBlock* dict has keys:
        ``block_type``, ``line_start`` (1-based), ``line_end`` (1-based),
        ``raw_lines``, ``preceded_by_commit``, ``followed_by_commit``,
        ``is_commented_out``.
    """
    total_lines = len(source_lines)
    comment_map = _build_comment_map(source_lines)
    execution_condition = detect_execution_condition(source_lines)

    raw_blocks: list[dict[str, Any]] = []
    idx = 0

    # Track commit positions for preceded_by / followed_by logic
    commit_indices: set[int] = set()
    for ci, line in enumerate(source_lines):
        if PATTERNS["COMMIT"].match(line) and not comment_map[ci]:
            commit_indices.add(ci)

    while idx < total_lines:
        line = source_lines[idx]

        # Skip blank / pure-comment lines for block detection
        stripped = line.strip()
        if not stripped:
            idx += 1
            continue
        if _is_comment_line(line) and not comment_map[idx]:
            idx += 1
            continue

        is_commented = comment_map[idx]

        # --- Detect SELECT INTO across current + next few lines ---
        select_into_detected = False
        if re.search(r'\bSELECT\b', line, re.IGNORECASE) and not any(
            PATTERNS[k].match(line) for k in _DML_STARTS
        ):
            # Peek ahead up to 5 lines to see if INTO follows
            peek_text = line
            for pi in range(1, min(6, total_lines - idx)):
                peek_text += " " + source_lines[idx + pi]
                if re.search(r'\bINTO\b', peek_text, re.IGNORECASE):
                    select_into_detected = True
                    break
                # Stop peeking if we hit a FROM before INTO
                if re.search(r'\bFROM\b', peek_text, re.IGNORECASE):
                    break

        block_type = _classify_line(line)

        if select_into_detected and block_type is None:
            block_type = "SELECT_INTO"

        if block_type is None:
            idx += 1
            continue

        block_start = idx
        block_end = find_block_end(source_lines, block_start, block_type)

        # Determine whether any line in the block is commented out
        block_is_commented = is_commented
        if not block_is_commented:
            # Check if ALL lines are inside block comments
            all_commented = all(
                comment_map[j] for j in range(block_start, block_end + 1)
                if j < len(comment_map)
            )
            if all_commented:
                block_is_commented = True

        raw_lines = source_lines[block_start:block_end + 1]

        raw_blocks.append({
            "block_type": block_type,
            "line_start": block_start + 1,   # 1-based
            "line_end": block_end + 1,        # 1-based
            "raw_lines": raw_lines,
            "preceded_by_commit": False,       # filled in below
            "followed_by_commit": False,       # filled in below
            "is_commented_out": block_is_commented,
        })

        idx = block_end + 1

    # --- Resolve commit adjacency ---
    for bi, block in enumerate(raw_blocks):
        block_end_0 = block["line_end"] - 1   # back to 0-based
        block_start_0 = block["line_start"] - 1

        # followed_by_commit: is there a COMMIT within the next 3 non-blank
        # lines after the block?
        scan_limit = min(block_end_0 + 4, total_lines)
        for ci in range(block_end_0 + 1, scan_limit):
            if ci in commit_indices:
                block["followed_by_commit"] = True
                break
            if source_lines[ci].strip() and not _is_comment_line(source_lines[ci]):
                break  # non-blank non-comment line that isn't COMMIT

        # preceded_by_commit: is there a COMMIT within the 3 lines before
        # the block?
        scan_start = max(block_start_0 - 3, 0)
        for ci in range(block_start_0 - 1, scan_start - 1, -1):
            if ci in commit_indices:
                block["preceded_by_commit"] = True
                break
            if source_lines[ci].strip() and not _is_comment_line(source_lines[ci]):
                break

    return {
        "function_name": function_name,
        "execution_condition": execution_condition,
        "raw_blocks": raw_blocks,
        "total_lines": total_lines,
    }
