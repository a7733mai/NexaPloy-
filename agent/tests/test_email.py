"""site.configure_email: Email Account setup through a fixed script, password on stdin."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nexaploy_agent.api import create_app
from nexaploy_agent.jobs.registry import REGISTRY
from nexaploy_agent.runtime import mail_relay as rm
from nexaploy_agent.runtime.docker import Call, Result

from .conftest import TOKEN, bench_running, make_bench, make_site

SITE = "a.example.com"
PASSWORD = "smtp-PASSWORD-very-secret"
PARAMS = {
    "site": SITE, "enabled": True, "email_id": "noreply@a.example.com", "smtp_server": "smtp.relay.test",
    "smtp_port": 587, "tls_mode": "starttls", "username": "relay-user", "password": PASSWORD,
}


def run_job(ctx, worker, kind, params):
    job = ctx.enqueue(kind, params, precheck=True)
    assert worker.run_once()
    return ctx.store.get(job["id"])


def setup_site(ctx, second_site=False):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", SITE, extra={"db_name": "_a", "db_password": "dbpw"})
    if second_site:
        make_site(ctx, "erp-v15", "b.example.com", extra={"db_name": "_b", "db_password": "x"})


def test_kind_registered():
    assert "site.configure_email" in REGISTRY


def test_params_validation(ctx):
    setup_site(ctx)
    cls = REGISTRY["site.configure_email"]
    clean = cls.validate_params(dict(PARAMS, email_id="NoReply@A.Example.com", smtp_server="SMTP.Relay.Test"))
    assert clean["email_id"] == "NoReply@a.example.com" and clean["smtp_server"] == "smtp.relay.test"
    for bad in (
        {"email_id": "not-an-email"},
        {"email_id": None},
        {"smtp_server": "bad host;rm"},
        {"tls_mode": "tls13"},
        {"smtp_port": 0},
        {"password": None},  # username without a password
        {"password": "a\nb"},
        {"account_name": "../x"},
    ):
        with pytest.raises(ValidationError):
            cls.validate_params(dict(PARAMS, **bad))
    # disabling needs nothing but the site
    assert cls.validate_params({"site": SITE, "enabled": False})["enabled"] is False
    # no authentication (relay allows the server IP)
    assert cls.validate_params(dict(PARAMS, username=None, password=None))["username"] is None


def test_bootstrap_args_never_carry_settings():
    args = rm.bootstrap_args(SITE)
    assert args[0] == "/home/frappe/frappe-bench/env/bin/python" and args[1] == "-c" and args[3:] == [SITE]
    compile(rm.BOOTSTRAP, "<bootstrap>", "exec")
    payload = json.loads(rm.stdin_payload(dict(PARAMS, account_name="Nexaploy Relay", verify_smtp=True, extra="x")))
    assert payload["password"] == PASSWORD and "extra" not in payload and "site" not in payload


def _ok(call: Call) -> Result:
    data = json.loads(call.input)
    assert data["password"] == PASSWORD and data["account_name"] == "Nexaploy Relay"
    out = {"ok": True, "account": "Nexaploy Relay", "email_id": data["email_id"], "created": True,
           "changed": ["enable_outgoing", "password"], "error": None}
    return Result(0, "some frappe warning\n" + rm.RESULT_MARKER + json.dumps(out) + "\n", "")


def test_configure_email_job(ctx, worker, runner):
    setup_site(ctx, second_site=True)  # fixed script: allowed on shared benches
    bench_running(runner)
    runner.on(["exec", "-T", "-w"], result=_ok)
    job = run_job(ctx, worker, "site.configure_email", PARAMS)
    assert job["status"] == "success", job
    assert job["result"]["created"] is True and job["result"]["email_id"] == "noreply@a.example.com"
    call = runner.find("exec -T -w")[0]
    assert call.args[:4] == ["docker", "compose", "-p", "erp-v15"]
    assert call.args[-1] == SITE
    # the password is only on stdin: not in argv, step output, or the stored params
    assert PASSWORD not in call.cmdline
    assert PASSWORD not in json.dumps(job)
    assert PASSWORD in call.input


def test_configure_email_failure_is_masked(ctx, worker, runner):
    setup_site(ctx)
    bench_running(runner)
    out = {"ok": False, "error": f"SMTPAuthenticationError: bad login {PASSWORD}",
           "traceback": f"Traceback ... {PASSWORD}"}
    runner.on(["exec", "-T", "-w"], stdout=rm.RESULT_MARKER + json.dumps(out) + "\n")
    job = run_job(ctx, worker, "site.configure_email", PARAMS)
    assert job["status"] == "failure" and "SMTPAuthenticationError" in job["error"]
    assert PASSWORD not in json.dumps(job)


def test_configure_email_needs_running_bench_and_result(ctx, worker, runner):
    setup_site(ctx)
    job = run_job(ctx, worker, "site.configure_email", PARAMS)
    assert job["status"] == "failure" and "not running" in job["error"]
    bench_running(runner)
    runner.on(["exec", "-T", "-w"], returncode=1, stderr="ModuleNotFoundError: frappe")
    job = run_job(ctx, worker, "site.configure_email", PARAMS)
    assert job["status"] == "failure" and "ModuleNotFoundError" in job["error"]


def test_api_endpoint(ctx):
    setup_site(ctx)
    client = TestClient(create_app(ctx))
    r = client.post(f"/v1/sites/{SITE}/email", json={k: v for k, v in PARAMS.items() if k != "site"},
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 202, r.text
    job = client.get(f"/v1/jobs/{r.json()['job_id']}", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert job["kind"] == "site.configure_email"
    assert PASSWORD not in json.dumps(job)


# --------------------------------------------------------------------------- the script itself
FAKE_FRAPPE = r'''
import json, os
STATE = os.environ["FAKE_STATE"]
FIELDS = ["email_id", "smtp_server", "smtp_port", "use_tls", "use_ssl_for_outgoing", "enable_outgoing",
          "default_outgoing", "always_use_account_email_id_as_sender", "enable_incoming", "default_incoming",
          "awaiting_password", "no_smtp_authentication", "login_id_is_different", "login_id", "domain",
          "service", "auth_method"]

def _load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"accounts": {}, "saves": [], "in_patch": []}

def _dump(data):
    json.dump(data, open(STATE, "w"))

class _Flags:
    in_patch = False

class _Local:
    flags = _Flags()
    db = None

local = _Local()

class _DB:
    def exists(self, doctype, name):
        return name in _load()["accounts"]
    def commit(self):
        pass
    def rollback(self):
        pass

db = _DB()

class _Meta:
    def has_field(self, f):
        return f in FIELDS

def get_meta(doctype):
    return _Meta()

class Doc(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)
    def set(self, k, v):
        self[k] = v
    def __setattr__(self, k, v):
        self[k] = v
    def __getattr__(self, k):
        return self.get(k)
    def get_password(self, field, raise_exception=True):
        return _load()["accounts"][self["email_account_name"]].get("_password")
    def _store(self):
        data = _load()
        rec = {k: v for k, v in self.items() if k != "password"}
        old = data["accounts"].get(self["email_account_name"], {})
        rec["_password"] = self.get("password") or old.get("_password")
        data["accounts"][self["email_account_name"]] = rec
        data["saves"].append(self["email_account_name"])
        data["in_patch"].append(local.flags.in_patch)
        _dump(data)
    def insert(self, ignore_permissions=False):
        self._store()
    def save(self, ignore_permissions=False):
        self._store()

def new_doc(doctype):
    return Doc()

def get_doc(doctype, name):
    rec = dict(_load()["accounts"][name])
    rec.pop("_password", None)
    d = Doc(rec)
    d["password"] = "*****"
    return d

def init(site, sites_path):
    pass

def connect():
    local.db = db

def destroy():
    pass
'''


def _run_script(tmp_path, cfg):
    (tmp_path / "frappe.py").write_text(FAKE_FRAPPE)
    env = {"PYTHONPATH": str(tmp_path), "FAKE_STATE": str(tmp_path / "state.json")}
    proc = subprocess.run([sys.executable, "-c", rm.BOOTSTRAP, SITE], input=json.dumps(cfg), cwd=tmp_path,
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    state = json.loads((tmp_path / "state.json").read_text()) if (tmp_path / "state.json").exists() else None
    return rm.parse_output(proc.stdout), state


def test_script_is_idempotent_with_fake_frappe(tmp_path):
    cfg = json.loads(rm.stdin_payload(dict(PARAMS, account_name="Nexaploy Relay", verify_smtp=True)))
    data, state = _run_script(tmp_path, cfg)
    assert data["ok"] and data["created"], data
    acc = state["accounts"]["Nexaploy Relay"]
    assert acc["enable_outgoing"] == 1 and acc["default_outgoing"] == 1 and acc["use_tls"] == 1
    assert not acc.get("use_ssl_for_outgoing") and acc["smtp_port"] == "587"
    assert acc["login_id_is_different"] == 1 and acc["login_id"] == "relay-user"
    assert acc["always_use_account_email_id_as_sender"] == 1 and acc["_password"] == PASSWORD
    assert state["in_patch"] == [False]  # verify_smtp: Frappe's SMTP check runs
    # second run: nothing changed -> no save
    data, state = _run_script(tmp_path, cfg)
    assert data["ok"] and not data["created"] and data["changed"] == [], data
    assert len(state["saves"]) == 1
    # new password + SSL, without the SMTP check
    cfg2 = dict(cfg, password="new-pass-123", tls_mode="ssl", smtp_port=465, verify_smtp=False)
    data, state = _run_script(tmp_path, cfg2)
    assert set(data["changed"]) == {"password", "use_tls", "use_ssl_for_outgoing", "smtp_port"}
    assert state["accounts"]["Nexaploy Relay"]["_password"] == "new-pass-123"
    assert state["in_patch"][-1] is True
    # disable: outgoing off, account kept
    data, state = _run_script(tmp_path, {"account_name": "Nexaploy Relay", "enabled": False})
    assert data["ok"] and set(data["changed"]) == {"enable_outgoing", "default_outgoing"}
    assert state["accounts"]["Nexaploy Relay"]["enable_outgoing"] == 0


def test_script_disable_without_account_is_noop(tmp_path):
    data, state = _run_script(tmp_path, {"account_name": "Nexaploy Relay", "enabled": False})
    assert data["ok"] and not data["created"] and state is None
