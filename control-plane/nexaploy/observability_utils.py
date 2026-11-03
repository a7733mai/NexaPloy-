"""Pure helpers for site observability (logs, analytics, slow queries, SQL playground, console).

No frappe import: unit-tested with plain pytest (tests/test_observability.py). The SQL validator
mirrors the agent's ``nexaploy_agent.runtime.sql.validate_query`` so the portal rejects a query
before a job is queued; the agent re-validates (it is the authority).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

OBS_KINDS = ("site.logs", "site.analytics", "site.slow_queries", "site.sql", "site.console")
LOG_TYPES = ("web", "worker", "scheduler", "frappe", "error")
DEFAULT_LINES = 200
MAX_LINES = 5000
MAX_DAYS = 31
MAX_QUERY_CHARS = 100_000
MAX_STATEMENTS = 50
MAX_ROWS = 5000
DEFAULT_ROWS = 500
SQL_TIMEOUT = 30
MAX_SQL_TIMEOUT = 300
MAX_CODE_CHARS = 100_000
CONSOLE_TIMEOUT = 60
MAX_CONSOLE_TIMEOUT = 600
ACTIVITY_TEXT_CHARS = 2000

READ_ONLY_KEYWORDS = frozenset({"select", "show", "describe", "desc", "explain", "with"})
CLIENT_COMMANDS = frozenset({
	"?", "charset", "clear", "connect", "delimiter", "edit", "ego", "exit", "go", "help",
	"nopager", "notee", "nowarning", "pager", "print", "prompt", "quit", "rehash", "sandbox",
	"source", "status", "system", "tee", "use", "warnings",
})
_WORD_RE = re.compile(r"[A-Za-z_?]+")
_INTO_FILE_RE = re.compile(r"\binto\s+(outfile|dumpfile)\b", re.IGNORECASE)
_LOCKING_RE = re.compile(r"\bfor\s+update\b|\block\s+in\s+share\s+mode\b", re.IGNORECASE)


class QueryError(ValueError):
	"""Rejected SQL (message is user-facing)."""


# --------------------------------------------------------------------------- permissions


def required_action(kind: str, read_only: bool = True) -> str:
	"""Team action (ARCHITECTURE.md §9) needed to run / read an observability job."""
	if kind in ("site.logs", "site.analytics", "site.slow_queries"):
		return "read"
	if kind == "site.sql":
		return "write" if read_only else "admin"
	if kind == "site.console":
		return "admin"
	raise ValueError(f"not an observability job: {kind}")


# --------------------------------------------------------------------------- SQL validator (mirror)


def _scan(sql: str) -> tuple[list[str], list[str]]:
	statements: list[str] = []
	problems: list[str] = []
	buf: list[str] = []
	i, n = 0, len(sql)
	line_start = True
	while i < n:
		ch = sql[i]
		nxt = sql[i + 1] if i + 1 < n else ""
		if line_start:
			j = i
			while j < n and sql[j] in " \t\r":
				j += 1
			m = _WORD_RE.match(sql, j)
			if m and m.group(0).lower() in CLIENT_COMMANDS:
				after = sql[m.end() : m.end() + 1]
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
					if j + 1 < n and sql[j + 1] == ch:
						j += 2
						continue
					break
				j += 1
			if j >= n:
				problems.append("unterminated quoted string")
				buf.append(sql[i:])
				i = n
			else:
				buf.append(sql[i : j + 1])
				i = j + 1
		elif ch == "/" and nxt == "*":
			end = sql.find("*/", i + 2)
			body = sql[i + 2 : end if end != -1 else n]
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
			i = n if end == -1 else end
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


def first_keyword(statement: str) -> str:
	m = re.match(r"[A-Za-z]+", statement.lstrip(" \t\r\n("))
	return m.group(0).lower() if m else ""


def validate_query(sql: Any, read_only: bool = True) -> list[str]:
	"""Statements of a playground query or QueryError (same rules as the agent)."""
	if not isinstance(sql, str) or not sql.strip():
		raise QueryError("Query is empty")
	if len(sql) > MAX_QUERY_CHARS:
		raise QueryError("Query is too long")
	if "\x00" in sql:
		raise QueryError("Query contains a NUL byte")
	statements, problems = _scan(sql)
	if problems:
		raise QueryError(problems[0])
	if not statements:
		raise QueryError("Query is empty")
	if len(statements) > MAX_STATEMENTS:
		raise QueryError("Too many statements")
	if read_only:
		for stmt in statements:
			word = first_keyword(stmt)
			if word not in READ_ONLY_KEYWORDS:
				raise QueryError(
					"Read-only mode allows only SELECT, SHOW, DESCRIBE, EXPLAIN and WITH statements"
				)
			if _INTO_FILE_RE.search(stmt):
				raise QueryError("INTO OUTFILE / DUMPFILE is not allowed")
			if _LOCKING_RE.search(stmt):
				raise QueryError("Locking reads are not allowed in read-only mode")
			if word == "with" and re.search(r"\)\s*(update|delete|insert|replace)\b", stmt, re.IGNORECASE):
				raise QueryError("Read-only mode allows only SELECT after WITH")
	return statements


# --------------------------------------------------------------------------- params


def _int(value: Any, default: int, low: int, high: int) -> int:
	try:
		n = int(value)
	except (TypeError, ValueError):
		n = default
	return max(low, min(n, high))


def to_bool(value: Any, default: bool = False) -> bool:
	if value is None or value == "":
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	return str(value).strip().lower() in ("1", "true", "yes", "on")


def logs_params(site: str, log: Any, lines: Any, since: Any = None) -> dict[str, Any]:
	log = (log or "web").strip() if isinstance(log, str) else "web"
	if log not in LOG_TYPES:
		raise ValueError("Unknown log")
	params: dict[str, Any] = {"site": site, "log": log, "lines": _int(lines, DEFAULT_LINES, 1, MAX_LINES)}
	if since:
		if not isinstance(since, str) or not re.match(
			r"^(\d{1,5}[smhd]|\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?Z?)?)$", since
		):
			raise ValueError("Invalid time filter")
		params["since"] = since
	return params


def analytics_params(site: str, days: Any, today: date) -> dict[str, Any]:
	n = _int(days, 7, 1, MAX_DAYS)
	return {"site": site, "date_from": (today - timedelta(days=n - 1)).isoformat(), "date_to": today.isoformat()}


def slow_query_params(site: str, limit: Any = 50, min_seconds: Any = 0) -> dict[str, Any]:
	try:
		secs = float(min_seconds or 0)
	except (TypeError, ValueError):
		secs = 0.0
	return {"site": site, "limit": _int(limit, 50, 1, 200), "min_seconds": max(0.0, min(secs, 3600.0))}


def sql_params(site: str, query: Any, read_only: Any = True, max_rows: Any = None, timeout: Any = None) -> dict[str, Any]:
	ro = to_bool(read_only, True)
	validate_query(query, ro)
	return {
		"site": site,
		"query": query,
		"read_only": ro,
		"max_rows": _int(max_rows, DEFAULT_ROWS, 1, MAX_ROWS),
		"timeout": _int(timeout, SQL_TIMEOUT, 1, MAX_SQL_TIMEOUT),
	}


def console_params(site: str, code: Any, commit: Any = False, timeout: Any = None) -> dict[str, Any]:
	if not isinstance(code, str) or not code.strip():
		raise ValueError("Code is empty")
	if len(code) > MAX_CODE_CHARS:
		raise ValueError("Code is too long")
	if "\x00" in code:
		raise ValueError("Code contains a NUL byte")
	return {
		"site": site,
		"code": code,
		"commit": to_bool(commit, False),
		"timeout": _int(timeout, CONSOLE_TIMEOUT, 1, MAX_CONSOLE_TIMEOUT),
	}


def same_request(a: dict[str, Any], b: dict[str, Any]) -> bool:
	"""Params equal for de-duplicating in-flight read-only jobs."""
	return {k: a.get(k) for k in set(a) | set(b)} == {k: b.get(k) for k in set(a) | set(b)}


# --------------------------------------------------------------------------- activity / results


def truncate(text: Any, limit: int = ACTIVITY_TEXT_CHARS) -> str:
	text = "" if text is None else str(text)
	return text if len(text) <= limit else text[:limit] + f"… [+{len(text) - limit} chars]"


def activity_details(kind: str, params: dict[str, Any]) -> dict[str, Any]:
	"""Team Activity details for an observability call (query/code text truncated)."""
	details = {k: v for k, v in params.items() if k not in ("site", "query", "code")}
	if kind == "site.sql":
		details["query"] = truncate(params.get("query"))
	elif kind == "site.console":
		details["code"] = truncate(params.get("code"))
	return details


def activity_action(kind: str) -> str:
	return kind  # "site.logs", "site.sql", ... (dotted verbs, ARCHITECTURE.md §10)


def rows_to_csv(columns: list[str], rows: list[list[Any]]) -> str:
	"""CSV text (RFC 4180, NULL -> empty). The portal builds CSV client-side; this mirrors it."""
	import csv
	import io

	buf = io.StringIO()
	w = csv.writer(buf, lineterminator="\r\n")
	w.writerow(columns)
	for r in rows:
		w.writerow(["" if v is None else v for v in r])
	return buf.getvalue()


def shape_result(kind: str, status: str, result: Any, error: str | None = None) -> dict[str, Any]:
	"""Portal payload for an observability job: ``{status, done, error, data}``.

	``data`` is the agent result with internal bookkeeping (``bench``, ``started*``) removed.
	"""
	done = status in ("Success", "Failure")
	data = dict(result) if isinstance(result, dict) else {}
	for key in ("bench", "started", "started_done"):
		data.pop(key, None)
	out: dict[str, Any] = {"kind": kind, "status": status, "done": done, "error": error if status == "Failure" else None}
	if not done or (status != "Success" and not data):
		out["data"] = None
		return out
	if kind == "site.sql":
		data.setdefault("columns", [])
		data.setdefault("rows", [])
		data.setdefault("truncated", False)
		data.setdefault("error", None)
	elif kind == "site.console":
		data.setdefault("output", "")
		data.setdefault("ok", status == "Success")
	elif kind == "site.logs":
		data.setdefault("sources", [])
	elif kind == "site.slow_queries":
		data.setdefault("queries", [])
	out["data"] = data
	return out


def chart_bars(points: list[dict[str, Any]], key: str, max_bars: int = 168) -> list[dict[str, Any]]:
	"""Scale ``points[key]`` to 0..100 for simple bar charts (latest ``max_bars`` points)."""
	pts = points[-max_bars:] if points else []
	peak = max((float(p.get(key) or 0) for p in pts), default=0) or 1
	return [{**p, "pct": round(100.0 * float(p.get(key) or 0) / peak, 1)} for p in pts]
