"""Outgoing e-mail relay for one site (``site.configure_email``).

Frappe sends mail through the default outgoing **Email Account** document, not site_config, so
the agent runs a small fixed script inside the bench's ``backend`` container (same mechanism as
``site.console``)::

    docker compose -p <b> ... exec -T -w /home/frappe/frappe-bench/sites backend \\
        /home/frappe/frappe-bench/env/bin/python -c <BOOTSTRAP> <site>

The settings, **including the SMTP password**, are written to the script's **stdin** as one JSON
object, so the password never appears in argv, ``ps`` output, step logs or files. The script
creates or updates the Email Account named ``account_name`` (default "Nexaploy Relay"):

* ``enabled``: enable_outgoing=1, default_outgoing=1, smtp server/port/TLS/SSL, login, password,
  ``always_use_account_email_id_as_sender``, ``email_id``; other accounts lose
  ``default_outgoing`` (Frappe's own ``there_must_be_only_one_default``).
* ``enabled=false``: the account stays (history) but enable_outgoing/default_outgoing are cleared.

Only fields that exist in the site's Email Account meta are set (v14/v15/v16 differ slightly).
With ``verify_smtp`` (default) Frappe's own validation connects to the SMTP server on save, so a
wrong password fails the job; otherwise the save runs with ``flags.in_patch`` (no connection).
It is idempotent: values are compared first and the document is saved only when something
changed. The outcome is printed as one JSON line after :data:`RESULT_MARKER`.
"""

from __future__ import annotations

import json
from typing import Any

from .console import CONTAINER_PYTHON, CONTAINER_SITES

RESULT_MARKER = "__NEXAPLOY_EMAIL_RESULT__"
DEFAULT_ACCOUNT_NAME = "Nexaploy Relay"
DEFAULT_TIMEOUT = 120
TLS_MODES = ("starttls", "ssl", "none")

BOOTSTRAP = r"""
import json, sys, traceback
site = sys.argv[1]
MARKER = "__NEXAPLOY_EMAIL_RESULT__"
cfg = json.loads(sys.stdin.read() or "{}")
result = {"ok": False, "changed": [], "created": False, "error": None, "account": cfg.get("account_name")}

def norm(value):
    return "" if value in (None, 0, "0", "", False) else str(value)

import frappe
try:
    frappe.init(site=site, sites_path=".")
    frappe.connect()
    name = cfg["account_name"]
    enabled = bool(cfg.get("enabled"))
    exists = frappe.db.exists("Email Account", name)
    if exists or enabled:
        if exists:
            doc = frappe.get_doc("Email Account", name)
        else:
            doc = frappe.new_doc("Email Account")
            doc.email_account_name = name
            result["created"] = True
        meta = frappe.get_meta("Email Account")
        mode = cfg.get("tls_mode") or "starttls"
        username = cfg.get("username") or ""
        values = {"enable_outgoing": 1 if enabled else 0, "default_outgoing": 1 if enabled else 0}
        if enabled:
            different = bool(username) and username != cfg["email_id"]
            values.update({
                "email_id": cfg["email_id"],
                "domain": None,
                "service": None,
                "smtp_server": cfg["smtp_server"],
                "smtp_port": str(cfg.get("smtp_port") or ""),
                "use_tls": 1 if mode == "starttls" else 0,
                "use_ssl_for_outgoing": 1 if mode == "ssl" else 0,
                "always_use_account_email_id_as_sender": 1,
                "enable_incoming": 0,
                "default_incoming": 0,
                "awaiting_password": 0,
                "no_smtp_authentication": 0 if username else 1,
                "login_id_is_different": 1 if different else 0,
                "login_id": username if different else None,
            })
            if meta.has_field("auth_method"):
                values["auth_method"] = "Basic"
        for field, value in values.items():
            if meta.has_field(field) and norm(doc.get(field)) != norm(value):
                doc.set(field, value)
                result["changed"].append(field)
        password = cfg.get("password")
        if enabled and password:
            stored = None
            if not result["created"]:
                try:
                    stored = doc.get_password("password", raise_exception=False)
                except Exception:
                    stored = None
            if stored != password:
                doc.password = password
                result["changed"].append("password")
        if result["changed"]:
            if not (enabled and cfg.get("verify_smtp", True)):
                frappe.local.flags.in_patch = True  # skip Frappe's SMTP login check on save
            try:
                if result["created"]:
                    doc.insert(ignore_permissions=True)
                else:
                    doc.save(ignore_permissions=True)
            finally:
                frappe.local.flags.in_patch = False
            frappe.db.commit()
        result["email_id"] = doc.get("email_id")
    result["ok"] = True
except BaseException as exc:
    result["error"] = ("%s: %s" % (type(exc).__name__, exc))[:2000]
    result["traceback"] = traceback.format_exc()[-4000:]
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
sys.stdout.write("\n" + MARKER + json.dumps(result) + "\n")
sys.stdout.flush()
""".strip() + "\n"


def bootstrap_args(site: str) -> list[str]:
    """argv (inside the container): the settings are passed on stdin, never here."""
    return [CONTAINER_PYTHON, "-c", BOOTSTRAP, site]


def stdin_payload(params: dict[str, Any]) -> str:
    """The JSON document written to the script's stdin."""
    keys = ("account_name", "enabled", "email_id", "smtp_server", "smtp_port", "tls_mode",
            "username", "password", "verify_smtp")
    return json.dumps({k: params.get(k) for k in keys})


def parse_output(stdout: str) -> dict[str, Any] | None:
    """The JSON line after :data:`RESULT_MARKER` (last one wins), or None."""
    idx = (stdout or "").rfind(RESULT_MARKER)
    if idx == -1:
        return None
    rest = stdout[idx + len(RESULT_MARKER):]
    line = rest.splitlines()[0] if rest else ""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


__all__ = ["BOOTSTRAP", "CONTAINER_SITES", "DEFAULT_ACCOUNT_NAME", "RESULT_MARKER", "TLS_MODES",
           "bootstrap_args", "parse_output", "stdin_payload"]
