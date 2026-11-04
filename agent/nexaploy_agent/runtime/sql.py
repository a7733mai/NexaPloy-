"""SQL playground for one site (``site.sql``, ARCHITECTURE.md §10 Phase 4 "observability").

Queries run in the shared MariaDB container with the **site's own** database user (``db_name`` /
``db_user`` + ``db_password`` from ``site_config.json``), never root::

    docker exec -i -e MYSQL_PWD nexaploy-db mariadb --sandbox --xml --user <u> --host 127.0.0.1 \\
        --protocol TCP --database <db>          # query text on stdin

* The password reaches the ``docker`` CLI only through its environment (``MYSQL_PWD``).
* ``--sandbox`` disables the client's ``system`` / ``source`` / ``tee`` commands (MariaDB >= 10.6.18);
  :func:`validate_query` also rejects client commands and backslash escapes outside literals.
* ``read_only`` (default): only SELECT / SHOW / DESCRIBE / EXPLAIN / WITH statements, wrapped in
  ``SET SESSION TRANSACTION READ ONLY; START TRANSACTION; ...; ROLLBACK;``.
* ``max_statement_time`` bounds every statement; ``sql_select_limit`` bounds rows sent back.
* Output is ``--xml`` rather than ``--batch --raw``: batch output cannot separate several result
  sets nor tell NULL from 'NULL', and raw values may contain tabs/newlines. The XML is parsed into
  ``{columns, rows, truncated}`` per result set.

Everything here except :func:`run_query` is pure (unit-tested).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Iterable

DB_CONTAINER = "nexaploy-db"
MAX_QUERY_CHARS = 100_000
MAX_STATEMENTS = 50
DEFAULT_MAX_ROWS = 500
MAX_ROWS = 5000
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
MAX_CELL_CHARS = 2000
MAX_RESULT_SETS = 10
MAX_OUTPUT_CHARS = 2_000_000  # whole result (cells), keeps job results small

READ_ONLY_KEYWORDS = frozenset({"select", "show", "describe", "desc", "explain", "with"})
# mariadb client commands (long form); recognised by the client at the start of a line.
CLIENT_COMMANDS = frozenset({
    "?", "charset", "clear", "connect", "delimiter", "edit", "ego", "exit", "go", "help",
    "nopager", "notee", "nowarning", "pager", "print", "prompt", "quit", "rehash", "sandbox",
    "source", "status", "system", "tee", "use", "warnings",
})
_WORD_RE = re.compile(r"[A-Za-z_?]+")
_INTO_FILE_RE = re.compile(r"\binto\s+(outfile|dumpfile)\b", re.IGNORECASE)
_LOCKING_RE = re.compile(r"\bfor\s+update\b|\block\s+in\s+share\s+mode\b", re.IGNORECASE)


class SqlValidationError(ValueError):
    """The query is not allowed (message is shown to the user)."""


# --------------------------------------------------------------------------- scanning
def _scan(sql: str) -> tuple[list[str], list[str]]:
    """Split ``sql`` on ``;`` outside quotes/comments.

    Returns ``(statements, problems)``: statements with comments removed (literals kept),
    problems = client commands / backslashes / executable comments found outside literals.
    """
    statements: list[str] = []
    problems: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    line_start = True
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if line_start:
            # first word of a line (outside literals) must not be a client command
            j = i
            while j < n and sql[j] in " \t\r":
                j += 1
            m = _WORD_RE.match(sql, j)
            if m and m.group(0).lower() in CLIENT_COMMANDS:
                after = sql[m.end():m.end() + 1]
                if after in ("", " ", "\t", "\r", "\n", ";"):
                    problems.append(f"client command {m.group(0)!r} is not allowed")
            line_start = False
        if ch == "\n":
            buf.append(ch)
            line_start = True
            i += 1
        elif ch in ("'", '"', "`"):
            j = i + 1
            while j < n:
                if sql[j] == "\\" and ch != "`":
                    j += 2
                    continue
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:  # doubled quote
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                problems.append("unterminated quoted string")
                buf.append(sql[i:])
                i = n
            else:
                buf.append(sql[i:j + 1])
                line_start = False
                i = j + 1
        elif ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            body = sql[i + 2:end if end != -1 else n]
            if body.startswith("!") or body.startswith("M!"):
                problems.append("executable comments (/*! ... */) are not allowed")
            if end == -1:
                problems.append("unterminated comment")
                i = n
            else:
                buf.append(" ")
                i = end + 2
        elif (ch == "-" and nxt == "-" and (i + 2 >= n or sql[i + 2] in " \t\r\n")) or ch == "#":
            end = sql.find("\n", i)
            i = n if end == -1 else end  # keep the newline
        elif ch == "\\":
            problems.append("backslash commands are not allowed")
            buf.append(ch)
            i += 1
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    stmt = "".join(buf).strip()
    if stmt:
        statements.append(stmt)
    return statements, problems


def split_statements(sql: str) -> list[str]:
    """Statements of ``sql`` (comments removed, ``;`` inside literals respected)."""
    return _scan(sql or "")[0]


def first_keyword(statement: str) -> str:
    s = statement.lstrip(" \t\r\n(")
    m = re.match(r"[A-Za-z]+", s)
    return m.group(0).lower() if m else ""


def validate_query(sql: Any, read_only: bool = True) -> list[str]:
    """Check a playground query; returns its statements or raises :class:`SqlValidationError`.

    All modes: non-empty, <= MAX_QUERY_CHARS, <= MAX_STATEMENTS, no mariadb client commands,
    backslashes outside literals or executable comments. ``read_only``: every statement starts
    with SELECT/SHOW/DESCRIBE/DESC/EXPLAIN/WITH and has no INTO OUTFILE/DUMPFILE or locking read.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SqlValidationError("query is empty")
    if len(sql) > MAX_QUERY_CHARS:
        raise SqlValidationError(f"query is longer than {MAX_QUERY_CHARS} characters")
    if "\x00" in sql:
        raise SqlValidationError("query contains a NUL byte")
    statements, problems = _scan(sql)
    if problems:
        raise SqlValidationError(problems[0])
    if not statements:
        raise SqlValidationError("query is empty")
    if len(statements) > MAX_STATEMENTS:
        raise SqlValidationError(f"at most {MAX_STATEMENTS} statements per run")
    if read_only:
        for stmt in statements:
            word = first_keyword(stmt)
            if word not in READ_ONLY_KEYWORDS:
                raise SqlValidationError(
                    f"read-only mode allows only SELECT, SHOW, DESCRIBE, EXPLAIN and WITH (got {word.upper() or '?'})"
                )
            if _INTO_FILE_RE.search(stmt):
                raise SqlValidationError("INTO OUTFILE / DUMPFILE is not allowed")
            if _LOCKING_RE.search(stmt):
                raise SqlValidationError("locking reads are not allowed in read-only mode")
            if word == "with" and re.search(r"\)\s*(update|delete|insert|replace)\b", stmt, re.IGNORECASE):
                raise SqlValidationError("read-only mode allows only SELECT after WITH")
    return statements


# --------------------------------------------------------------------------- script / command
def build_script(statements: Iterable[str], read_only: bool, timeout: int, max_rows: int) -> str:
    """The text written to the client's stdin."""
    lines = [
        f"SET SESSION max_statement_time = {int(timeout)};",
        # one row more than shown, so truncation is detectable
        f"SET SESSION sql_select_limit = {int(max_rows) + 1};",
    ]
    if read_only:
        lines += ["SET SESSION TRANSACTION READ ONLY;", "START TRANSACTION;"]
    lines += [f"{s};" for s in statements]
    if read_only:
        lines.append("ROLLBACK;")
    return "\n".join(lines) + "\n"


def db_credentials(site_config: dict[str, Any]) -> tuple[str, str, str]:
    """(user, password, database) of the site; ``db_user`` defaults to ``db_name`` (Frappe)."""
    db_name = site_config.get("db_name")
    password = site_config.get("db_password")
    if not isinstance(db_name, str) or not re.match(r"^[A-Za-z0-9_]{1,64}$", db_name):
        raise SqlValidationError("site_config.json has no valid db_name")
    if not isinstance(password, str) or not password:
        raise SqlValidationError("site_config.json has no db_password")
    user = site_config.get("db_user") or db_name
    if not isinstance(user, str) or not re.match(r"^[A-Za-z0-9_]{1,80}$", user):
        raise SqlValidationError("site_config.json has an invalid db_user")
    return user, password, db_name


def sql_command(user: str, database: str, container: str = DB_CONTAINER) -> list[str]:
    """argv for the query; the password comes from the env var MYSQL_PWD, the SQL from stdin."""
    return [
        "docker", "exec", "-i", "-e", "MYSQL_PWD", container,
        "mariadb", "--sandbox", "--xml",
        "--user", user, "--host", "127.0.0.1", "--protocol", "TCP",
        "--database", database, "--default-character-set", "utf8mb4",
    ]


# --------------------------------------------------------------------------- output parsing
def _cell(value: str | None, max_chars: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if len(value) > max_chars:
        return value[:max_chars], True
    return value, False


def parse_xml_output(text: str, max_rows: int = DEFAULT_MAX_ROWS,
                     max_cell_chars: int = MAX_CELL_CHARS,
                     max_total_chars: int = MAX_OUTPUT_CHARS) -> list[dict[str, Any]]:
    """``mariadb --xml`` output -> ``[{statement, columns, rows, row_count, truncated}]``.

    The client prints one ``<resultset>`` document per statement that returns rows. NULL is
    ``<field name="x" xsi:nil="true" />`` -> ``None``. Rows beyond ``max_rows`` are dropped
    (``truncated``); long cells are cut to ``max_cell_chars`` (``cells_truncated``).
    """
    results: list[dict[str, Any]] = []
    total = 0
    # each statement emits its own "<?xml ...?><resultset>...</resultset>" block
    for m in re.finditer(r"<resultset\b.*?</resultset>|<resultset\b[^>]*/>", text or "", re.S):
        try:
            root = ET.fromstring(m.group(0))
        except ET.ParseError:
            continue
        columns: list[str] = []
        rows: list[list[str | None]] = []
        truncated = False
        cells_truncated = False
        count = 0
        for row in root.findall("row"):
            count += 1
            fields = row.findall("field")
            if not columns:
                columns = [f.get("name", "") for f in fields]
            if len(rows) >= max_rows or total >= max_total_chars:
                truncated = True
                continue
            out_row: list[str | None] = []
            for f in fields:
                nil = any(k.endswith("nil") and v == "true" for k, v in f.attrib.items())
                value, cut = _cell(None if nil else (f.text or ""), max_cell_chars)
                cells_truncated = cells_truncated or cut
                total += len(value or "")
                out_row.append(value)
            rows.append(out_row)
        results.append({
            "statement": (root.get("statement") or "")[:500],
            "columns": columns,
            "rows": rows,
            "row_count": count,
            "truncated": truncated,
            "cells_truncated": cells_truncated,
        })
        if len(results) >= MAX_RESULT_SETS:
            break
    return results


def shape_result(results: list[dict[str, Any]], statements: list[str], read_only: bool,
                 max_rows: int) -> dict[str, Any]:
    """Job result: the last result set at top level (``columns/rows/truncated``) + all sets."""
    last = results[-1] if results else {"columns": [], "rows": [], "truncated": False, "row_count": 0}
    truncated = bool(last.get("truncated"))
    rows = last.get("rows") or []
    # sql_select_limit returns max_rows + 1 rows when there are more
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        truncated = True
    return {
        "columns": last.get("columns") or [],
        "rows": rows,
        "truncated": truncated,
        "row_count": len(rows),
        "result_sets": results,
        "statements": len(statements),
        "read_only": read_only,
        "max_rows": max_rows,
    }


def clean_error(stderr: str) -> str:
    """Last 'ERROR ...' line(s) of the client's stderr."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("ERROR")]
    return "\n".join((errors or lines)[-3:])[:2000]


# --------------------------------------------------------------------------- execution
def run_query(runner, site_config: dict[str, Any], query: str, *, read_only: bool = True,
              max_rows: int = DEFAULT_MAX_ROWS, timeout: int = DEFAULT_TIMEOUT,
              container: str = DB_CONTAINER) -> dict[str, Any]:
    """Validate and run ``query``. Returns the shaped result with ``error`` (None on success;
    the client's error message otherwise - result sets before the failing statement are kept).

    Raises :class:`SqlValidationError` for a rejected query / bad site config.
    """
    max_rows = max(1, min(int(max_rows), MAX_ROWS))
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    statements = validate_query(query, read_only=read_only)
    user, password, database = db_credentials(site_config)
    script = build_script(statements, read_only, timeout, max_rows)
    res = runner.run(sql_command(user, database, container), env={"MYSQL_PWD": password},
                     input=script, timeout=timeout + 30)
    out = shape_result(parse_xml_output(res.stdout, max_rows=max_rows + 1), statements, read_only, max_rows)
    out["error"] = None
    if not res.ok:
        err = clean_error(res.stderr) or f"mariadb exited with {res.returncode}"
        out["error"] = err.replace(password, "********") if len(password) >= 4 else err
    return out
