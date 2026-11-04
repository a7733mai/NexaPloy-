"""Python console for one site (``site.console``, ARCHITECTURE.md §10 "observability").

The user's code is written to ``benches/<b>/sites/.nexaploy-console/<job>.py`` (dir 700, file 600,
owner 3113, removed afterwards) and executed inside the bench's ``backend`` container::

    docker compose -p <b> ... exec -T -w /home/frappe/frappe-bench/sites backend \\
        /home/frappe/frappe-bench/env/bin/python -c <BOOTSTRAP> <site> <file> <commit> <timeout> <max_output>

(``bench --site s execute`` only calls a dotted function, so a small bootstrap is used instead.)
The bootstrap does ``frappe.init(site, sites_path='.')``, ``frappe.connect()``, ``exec(code)``
with stdout/stderr captured (capped), then ``frappe.db.commit()`` when ``commit`` else
``frappe.db.rollback()``, and ``frappe.destroy()``. ``signal.alarm`` enforces the timeout inside
the container (the agent also bounds the ``docker`` call). The outcome is printed as one JSON line
after :data:`RESULT_MARKER`.
"""

from __future__ import annotations

import json
from typing import Any

CONSOLE_DIR = ".nexaploy-console"
CONTAINER_BENCH = "/home/frappe/frappe-bench"
CONTAINER_SITES = f"{CONTAINER_BENCH}/sites"
CONTAINER_PYTHON = f"{CONTAINER_BENCH}/env/bin/python"
RESULT_MARKER = "__NEXAPLOY_CONSOLE_RESULT__"
MAX_CODE_CHARS = 100_000
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 1_000_000

BOOTSTRAP = r'''
import io, json, signal, sys, time, traceback
site, path, commit, timeout, limit = sys.argv[1], sys.argv[2], sys.argv[3] == "1", int(sys.argv[4]), int(sys.argv[5])
MARKER = "__NEXAPLOY_CONSOLE_RESULT__"

class ConsoleTimeout(BaseException):
    pass

class Capped(io.TextIOBase):
    def __init__(self, limit):
        self.parts, self.size, self.limit, self.truncated = [], 0, limit, False
    def writable(self):
        return True
    def write(self, s):
        s = str(s)
        room = self.limit - self.size
        if room <= 0:
            self.truncated = self.truncated or bool(s)
            return len(s)
        if len(s) > room:
            self.truncated = True
        self.parts.append(s[:room])
        self.size += min(len(s), room)
        return len(s)
    def getvalue(self):
        return "".join(self.parts)

def _alarm(signum, frame):
    raise ConsoleTimeout("timed out after %ss" % timeout)

out = Capped(limit)
result = {"ok": False, "committed": False, "error": None}
started = time.time()
real_stdout = sys.stdout
signal.signal(signal.SIGALRM, _alarm)
signal.alarm(timeout)
import frappe
try:
    frappe.init(site=site, sites_path=".")
    frappe.connect()
    with open(path) as fh:
        code = fh.read()
    sys.stdout, sys.stderr = out, out
    try:
        exec(compile(code, "<console>", "exec"), {"frappe": frappe, "__name__": "__console__"})
    finally:
        sys.stdout, sys.stderr = real_stdout, sys.__stderr__
    signal.alarm(0)
    if commit:
        frappe.db.commit()
        result["committed"] = True
    else:
        frappe.db.rollback()
    result["ok"] = True
except BaseException as exc:
    signal.alarm(0)
    sys.stdout, sys.stderr = real_stdout, sys.__stderr__
    out.write(traceback.format_exc())
    result["error"] = ("%s: %s" % (type(exc).__name__, exc))[:2000]
    try:
        if getattr(frappe.local, "db", None):
            frappe.db.rollback()
    except BaseException:
        pass
finally:
    try:
        frappe.destroy()
    except BaseException:
        pass
result["output"] = out.getvalue()
result["truncated"] = out.truncated
result["duration"] = round(time.time() - started, 3)
real_stdout.write("\n" + MARKER + json.dumps(result) + "\n")
real_stdout.flush()
'''.strip() + "\n"


def console_file_name(job_id: str) -> str:
    safe = "".join(c for c in job_id if c.isalnum()) or "job"
    return f"{safe}.py"


def bootstrap_args(site: str, container_path: str, commit: bool, timeout: int,
                   max_output: int = MAX_OUTPUT_CHARS) -> list[str]:
    """argv (inside the container) that runs the bootstrap for one console file."""
    return [
        CONTAINER_PYTHON, "-c", BOOTSTRAP,
        site, container_path, "1" if commit else "0", str(int(timeout)), str(int(max_output)),
    ]


def parse_console_output(stdout: str) -> dict[str, Any] | None:
    """The JSON line after :data:`RESULT_MARKER` (last one wins), or None."""
    idx = (stdout or "").rfind(RESULT_MARKER)
    if idx == -1:
        return None
    line = stdout[idx + len(RESULT_MARKER):].splitlines()[0] if stdout[idx + len(RESULT_MARKER):] else ""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def preamble(stdout: str) -> str:
    """Anything printed before the marker (frappe warnings etc.)."""
    idx = (stdout or "").rfind(RESULT_MARKER)
    return (stdout if idx == -1 else stdout[:idx]).strip()
