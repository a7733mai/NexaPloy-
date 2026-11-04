"""Smoke checks of one site (``site.smoke_check``, "Safe update" test in the control plane).

Both probes run inside the bench's ``backend`` container (§5.4), so they test exactly the code
and image the site runs on, without going through DNS, TLS or Traefik:

- HTTP: ``python -c HTTP_PROBE <site> <timeout> <path>...`` requests
  ``http://127.0.0.1:8000<path>`` (gunicorn) with ``Host: <site>`` and reports status, time and
  a short body snippet per path;
- database: ``python -c DB_PROBE <site>`` does ``frappe.init`` / ``frappe.connect``,
  ``frappe.get_all("User", limit=1)`` and ``frappe.get_installed_apps()`` (read only, nothing
  is committed).

Each probe prints one JSON line after :data:`RESULT_MARKER`. Everything else here is pure and
unit-tested.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

CONTAINER_BENCH = "/home/frappe/frappe-bench"
CONTAINER_SITES = f"{CONTAINER_BENCH}/sites"
CONTAINER_PYTHON = f"{CONTAINER_BENCH}/env/bin/python"
RESULT_MARKER = "__NEXAPLOY_SMOKE_RESULT__"
DEFAULT_PATHS = ("/api/method/ping", "/login")
DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 120
MAX_PATHS = 10
PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,200}$")
SNIPPET_CHARS = 300

HTTP_PROBE = r'''
import json, sys, time, urllib.error, urllib.request
site, timeout, paths = sys.argv[1], float(sys.argv[2]), sys.argv[3:]
out = []
for path in paths:
    req = urllib.request.Request("http://127.0.0.1:8000" + path,
                                 headers={"Host": site, "User-Agent": "nexaploy-smoke-check"})
    started = time.time()
    status, body, error = None, b"", None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, body = resp.status, resp.read(65536)
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            body = exc.read(4096)
        except Exception:
            body = b""
    except Exception as exc:
        error = ("%s: %s" % (type(exc).__name__, exc))[:500]
    out.append({"path": path, "status": status, "ms": int((time.time() - started) * 1000),
                "bytes": len(body), "error": error, "snippet": body[:300].decode("utf-8", "replace")})
print("__NEXAPLOY_SMOKE_RESULT__" + json.dumps(out))
'''.strip() + "\n"

DB_PROBE = r'''
import json, sys, time
site = sys.argv[1]
result = {"ok": False, "error": None}
started = time.time()
try:
    import frappe
    frappe.init(site=site, sites_path=".")
    frappe.connect()
    result["users"] = len(frappe.get_all("User", fields=["name"], limit=1))
    result["installed_apps"] = list(frappe.get_installed_apps())
    result["ok"] = True
except BaseException as exc:
    result["error"] = ("%s: %s" % (type(exc).__name__, exc))[:2000]
finally:
    try:
        frappe.db.rollback()
    except BaseException:
        pass
    try:
        frappe.destroy()
    except BaseException:
        pass
result["duration"] = round(time.time() - started, 3)
print("__NEXAPLOY_SMOKE_RESULT__" + json.dumps(result))
'''.strip() + "\n"


def valid_path(path: str) -> str:
    path = str(path or "").strip()
    if not PATH_RE.match(path) or ".." in path:
        raise ValueError(f"invalid smoke check path {path!r}")
    return path


def http_args(site: str, paths: Iterable[str], timeout: int) -> list[str]:
    """argv (inside the container) of the HTTP probe."""
    return [CONTAINER_PYTHON, "-c", HTTP_PROBE, site, str(int(timeout)), *[valid_path(p) for p in paths]]


def db_args(site: str) -> list[str]:
    return [CONTAINER_PYTHON, "-c", DB_PROBE, site]


def parse_output(stdout: str) -> Any:
    """The JSON after the last :data:`RESULT_MARKER`, or None."""
    text = stdout or ""
    idx = text.rfind(RESULT_MARKER)
    if idx == -1:
        return None
    rest = text[idx + len(RESULT_MARKER):].splitlines()
    try:
        return json.loads(rest[0]) if rest else None
    except (json.JSONDecodeError, ValueError):
        return None


def evaluate_http(probes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Probe results -> checks ``[{name, ok, detail}]``.

    Every path must answer HTTP 200; ``/api/method/ping`` must also say ``pong``.
    """
    checks = []
    for p in probes or []:
        path = p.get("path") or "?"
        status = p.get("status")
        ok = status == 200 and not p.get("error")
        detail = f"HTTP {status}" if status is not None else (p.get("error") or "no response")
        if ok and path.startswith("/api/method/ping") and "pong" not in (p.get("snippet") or ""):
            ok, detail = False, f"HTTP {status} without 'pong'"
        if p.get("ms") is not None:
            detail += f" in {p['ms']} ms"
        if not ok and p.get("snippet"):
            detail += ": " + " ".join(str(p["snippet"]).split())[:200]
        checks.append({"name": f"http {path}", "ok": ok, "detail": detail})
    return checks


def evaluate_db(data: dict[str, Any] | None, expected_apps: Iterable[str] = ()) -> list[dict[str, Any]]:
    """DB probe result -> checks (query + every expected app installed)."""
    if not isinstance(data, dict):
        return [{"name": "database", "ok": False, "detail": "the probe did not report a result"}]
    if not data.get("ok"):
        return [{"name": "database", "ok": False, "detail": data.get("error") or "failed"}]
    checks = [{"name": "database", "ok": True,
               "detail": f"frappe.get_all('User', limit=1) -> {data.get('users')} row(s) in {data.get('duration')}s"}]
    expected = [a for a in expected_apps if a]
    if expected:
        installed = set(data.get("installed_apps") or [])
        missing = [a for a in expected if a not in installed]
        checks.append({"name": "installed apps", "ok": not missing,
                       "detail": "missing: " + ", ".join(missing) if missing else ", ".join(expected)})
    return checks


def summary(checks: list[dict[str, Any]]) -> str:
    failed = [c for c in checks if not c.get("ok")]
    if not checks:
        return "no checks ran"
    if not failed:
        return f"{len(checks)} check(s) passed"
    return "; ".join(f"{c['name']}: {c.get('detail')}" for c in failed)
