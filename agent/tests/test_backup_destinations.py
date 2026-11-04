"""Backup destinations: Google Drive / OneDrive / Dropbox rclone remotes and backup.test_remote."""

import configparser
import json
import os
import stat

import pytest

from nexaploy_agent.jobs.server_jobs import oauth_token
from nexaploy_agent.redact import MASK
from nexaploy_agent.runtime.backup import oauth_sections, plan_backup_remote

from .conftest import run_job

ACCESS = "ya29.ACCESS-token-123"
REFRESH = "1//REFRESH-token-456"
TOKEN = json.dumps({"access_token": ACCESS, "token_type": "Bearer", "refresh_token": REFRESH,
                    "expiry": "2026-11-04T18:00:00Z"})
CLIENT_SECRET = "GOCSPX-client-SECRET"

DRIVE = {
    "remote": "nexaploy-backups", "type": "drive", "token": TOKEN,
    "client_id": "123-abc.apps.googleusercontent.com", "client_secret": CLIENT_SECRET,
    "prefix": "nexaploy-backups/srv-abc12345",
}
ONEDRIVE = {
    "remote": "nexaploy-backups", "type": "onedrive", "token": TOKEN, "drive_id": "b!AbC-12_x",
    "drive_type": "business", "client_id": "0000-1111", "client_secret": CLIENT_SECRET,
    "prefix": "nexaploy-backups/srv-abc12345",
}
DROPBOX = {"remote": "nexaploy-backups", "type": "dropbox", "token": TOKEN, "prefix": "nx/srv-abc12345"}
S3 = {
    "remote": "nexaploy-backups", "type": "s3", "provider": "AWS", "bucket": "nx-backups",
    "access_key_id": "AKIAEXAMPLE", "secret_access_key": "s3-SECRET-key", "prefix": "srv-abc12345",
}


def conf(settings):
    cp = configparser.RawConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read_string(settings.rclone_conf.read_text())
    return cp


def all_output(job):
    return "".join(s.get("output") or "" for s in job["steps"])


# ---- token validation ---------------------------------------------------------------------
def test_oauth_token_normalised():
    out = json.loads(oauth_token(TOKEN))
    assert out == {"access_token": ACCESS, "token_type": "Bearer", "refresh_token": REFRESH,
                   "expiry": "2026-11-04T18:00:00Z"}
    # dict input, extra keys dropped, default token_type
    out = json.loads(oauth_token({"access_token": "a", "refresh_token": "r", "scope": "x"}))
    assert out == {"access_token": "a", "refresh_token": "r", "token_type": "Bearer"}
    assert "\n" not in oauth_token(TOKEN)


@pytest.mark.parametrize("bad", [
    "not json", "[1, 2]", json.dumps({"access_token": "a"}),            # no refresh token
    json.dumps({"refresh_token": "r"}), json.dumps({"access_token": "", "refresh_token": "r"}),
    json.dumps({"access_token": "a\n[evil]", "refresh_token": "r"}),
    json.dumps({"access_token": "a", "refresh_token": "r", "expiry": "tomorrow"}),
    json.dumps({"access_token": 1, "refresh_token": "r"}), "x" * 20000,
])
def test_oauth_token_rejects(bad):
    with pytest.raises(ValueError):
        oauth_token(bad)


@pytest.mark.parametrize("bad", [
    {"type": "gdrive"}, {"token": None}, {"token": "{}"}, {"bucket": "nx-backups"},
    {"client_id": "x y"}, {"client_secret": None}, {"root_folder_id": "../x"},
    {"drive_id": "abc"}, {"prefix": "../up"}, {"remote": "a b"}, {"client_secret": "a\nb"},
])
def test_configure_oauth_validation(ctx, bad):
    with pytest.raises(Exception):
        ctx.enqueue("server.configure_backups", {**DRIVE, **bad})


@pytest.mark.parametrize("bad", [{"drive_id": None}, {"drive_type": "team"}, {"drive_id": "a b"},
                                 {"tenant": "x;y"}])
def test_configure_onedrive_validation(ctx, bad):
    with pytest.raises(Exception):
        ctx.enqueue("server.configure_backups", {**ONEDRIVE, **bad})


def test_s3_still_requires_keys(ctx):
    for missing in ("bucket", "access_key_id", "secret_access_key"):
        params = {k: v for k, v in S3.items() if k != missing}
        with pytest.raises(Exception):
            ctx.enqueue("server.configure_backups", params)
    with pytest.raises(Exception):
        ctx.enqueue("server.configure_backups", {**S3, "token": TOKEN})


# ---- rclone.conf -------------------------------------------------------------------------
@pytest.mark.parametrize("params,backend", [(DRIVE, "drive"), (ONEDRIVE, "onedrive"), (DROPBOX, "dropbox")])
def test_configure_oauth_writes_rclone_conf(ctx, worker, settings, params, backend):
    settings.rclone_conf.write_text("[other]\ntype = local\n")
    job = run_job(ctx, worker, "server.configure_backups", params)
    assert job["status"] == "success", job
    assert stat.S_IMODE(os.stat(settings.rclone_conf).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(settings.rclone_state).st_mode) == 0o600
    cp = conf(settings)
    assert cp.get("other", "type") == "local"
    section = dict(cp.items(f"nexaploy-backups-{backend}"))
    assert section["type"] == backend
    assert json.loads(section["token"])["refresh_token"] == REFRESH
    # same remote path layout as S3: <remote>:<prefix>/<bench>/<site>/<ts> through the alias
    assert dict(cp.items("nexaploy-backups")) == {"type": "alias", "remote": f"nexaploy-backups-{backend}:"}
    if backend == "drive":
        assert section["scope"] == "drive.file"
        assert section["client_id"] == DRIVE["client_id"] and section["client_secret"] == CLIENT_SECRET
    if backend == "onedrive":
        assert section["drive_id"] == "b!AbC-12_x" and section["drive_type"] == "business"
    assert settings.rclone_remote_configured()
    assert settings.backup_prefix == params["prefix"]
    # secrets: masked in output and in the stored params
    out = all_output(job)
    for secret in (ACCESS, REFRESH, CLIENT_SECRET):
        assert secret not in out and secret not in json.dumps(job["result"])
    stored = ctx.store.get(job["id"])["params"]
    assert stored["token"] == MASK
    if "client_secret" in params:
        assert stored["client_secret"] == MASK


def test_switching_destination_removes_old_credentials(ctx, worker, settings):
    run_job(ctx, worker, "server.configure_backups", S3)
    assert conf(settings).has_section("nexaploy-backups-s3")
    run_job(ctx, worker, "server.configure_backups", DRIVE)
    cp = conf(settings)
    assert not cp.has_section("nexaploy-backups-s3")
    assert "s3-SECRET-key" not in settings.rclone_conf.read_text()
    assert cp.get("nexaploy-backups", "remote") == "nexaploy-backups-drive:"
    run_job(ctx, worker, "server.configure_backups", S3)
    cp = conf(settings)
    assert not cp.has_section("nexaploy-backups-drive") and REFRESH not in settings.rclone_conf.read_text()
    assert cp.get("nexaploy-backups", "remote") == "nexaploy-backups-s3:nx-backups"
    assert not settings.rclone_state.exists()


def _rclone_refreshes(settings, new_access="ya29.REFRESHED"):
    """Simulate rclone writing a refreshed token back into rclone.conf."""
    cp = conf(settings)
    tok = json.loads(cp.get("nexaploy-backups-drive", "token"))
    tok.update({"access_token": new_access, "expiry": "2026-11-05T10:00:00Z"})
    cp.set("nexaploy-backups-drive", "token", json.dumps(tok))
    with open(settings.rclone_conf, "w") as fh:
        cp.write(fh)
    return tok


def test_refreshed_token_is_not_clobbered(ctx, worker, settings):
    run_job(ctx, worker, "server.configure_backups", DRIVE)
    refreshed = _rclone_refreshes(settings)
    # control plane re-pushes the same connection (e.g. server re-activated): keep rclone's token
    job = run_job(ctx, worker, "server.configure_backups", DRIVE)
    assert job["status"] == "success"
    assert json.loads(conf(settings).get("nexaploy-backups-drive", "token")) == refreshed
    assert "keeping the OAuth token" in all_output(job)
    # a non-token change (folder) is applied, the refreshed token still kept
    run_job(ctx, worker, "server.configure_backups", {**DRIVE, "root_folder_id": "FolderId_1"})
    cp = conf(settings)
    assert cp.get("nexaploy-backups-drive", "root_folder_id") == "FolderId_1"
    assert json.loads(cp.get("nexaploy-backups-drive", "token")) == refreshed
    # a reconnect (new token from the control plane) replaces it
    new_token = json.dumps({"access_token": "ya29.NEW", "refresh_token": "1//NEW-refresh"})
    run_job(ctx, worker, "server.configure_backups", {**DRIVE, "token": new_token})
    tok = json.loads(conf(settings).get("nexaploy-backups-drive", "token"))
    assert tok["refresh_token"] == "1//NEW-refresh"


def test_missing_state_file_writes_control_plane_token(ctx, worker, settings):
    run_job(ctx, worker, "server.configure_backups", DRIVE)
    _rclone_refreshes(settings)
    settings.rclone_state.unlink()
    run_job(ctx, worker, "server.configure_backups", DRIVE)
    assert json.loads(conf(settings).get("nexaploy-backups-drive", "token"))["access_token"] == ACCESS


def test_configure_oauth_idempotent(ctx, worker, settings):
    run_job(ctx, worker, "server.configure_backups", DROPBOX)
    mtime = settings.rclone_conf.stat().st_mtime_ns
    job = run_job(ctx, worker, "server.configure_backups", DROPBOX)
    assert job["status"] == "success"
    assert settings.rclone_conf.stat().st_mtime_ns == mtime
    assert all("already" in s["output"] for s in job["steps"])


def test_plan_backup_remote_pure():
    sections = oauth_sections("r", "dropbox", token=TOKEN)
    text, state, kept = plan_backup_remote("", "r", "dropbox", sections, {})
    assert not kept and "r-dropbox" in state
    disk = text.replace(ACCESS, "fresh-access")
    text2, _, kept = plan_backup_remote(disk, "r", "dropbox", sections, state)
    assert kept and "fresh-access" in text2
    # another type in the section on disk -> rewritten
    other = disk.replace("type = dropbox", "type = drive")
    text3, _, kept = plan_backup_remote(other, "r", "dropbox", sections, state)
    assert not kept and ACCESS in text3


# ---- backup.test_remote ------------------------------------------------------------------
def test_test_remote_configured(ctx, worker, runner, settings):
    run_job(ctx, worker, "server.configure_backups", DRIVE)
    runner.on(["rclone", "--config", str(settings.rclone_conf), "lsf"], stdout="erp-v15/\n")
    runner.on(["rclone", "--config", str(settings.rclone_conf), "about"],
              stdout=json.dumps({"total": 100, "used": 40, "free": 60}))
    job = run_job(ctx, worker, "backup.test_remote", {})
    assert job["status"] == "success", job
    base = f"nexaploy-backups:{DRIVE['prefix']}"
    cmds = [c.args[3:] for c in runner.find("rclone")]
    assert ["mkdir", base] in cmds
    assert ["touch", f"{base}/.nexaploy-connection-test"] in cmds
    assert ["deletefile", f"{base}/.nexaploy-connection-test"] in cmds
    assert job["result"]["ok"] is True and job["result"]["entries"] == 1
    assert job["result"]["about"] == {"total": 100, "used": 40, "free": 60}


def test_test_remote_unconfigured_fails(ctx, worker):
    job = run_job(ctx, worker, "backup.test_remote", {})
    assert job["status"] == "failure" and "not configured" in job["error"]


def test_test_remote_with_destination_uses_temp_config(ctx, worker, runner, settings):
    settings.rclone_conf.write_text("[nexaploy-backups]\ntype = local\n")
    before = settings.rclone_conf.read_text()
    seen = {}

    def capture(call):
        path = call.args[2]
        seen["path"] = path
        seen["text"] = open(path).read()
        seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        from nexaploy_agent.runtime.docker import Result
        return Result(0, "", "")

    runner.on(["rclone", "--config"], result=capture)
    job = run_job(ctx, worker, "backup.test_remote", {"destination": ONEDRIVE})
    assert job["status"] == "success", job
    assert seen["path"] != str(settings.rclone_conf) and seen["mode"] == 0o600
    assert "type = onedrive" in seen["text"]
    assert not os.path.exists(seen["path"])  # removed afterwards
    assert settings.rclone_conf.read_text() == before  # active remote untouched
    assert ctx.store.get(job["id"])["params"]["destination"]["token"] == MASK
    assert REFRESH not in all_output(job)


def test_test_remote_failure_reports_and_cleans_up(ctx, worker, runner, settings):
    runner.on(["rclone", "--config"], returncode=1, stderr=f"couldn't fetch token {REFRESH}")
    job = run_job(ctx, worker, "backup.test_remote", {"destination": DROPBOX})
    assert job["status"] == "failure"
    assert "rclone mkdir" in job["error"] and REFRESH not in job["error"]
    assert not list(settings.config_dir.glob(".rclone-test-*"))


def test_api_backup_test_route(settings, ctx):
    from fastapi.testclient import TestClient

    from nexaploy_agent.api import create_app

    from .conftest import TOKEN as API_TOKEN

    client = TestClient(create_app(ctx))
    r = client.post("/v1/server/backup-test", json={"destination": DROPBOX},
                    headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert r.status_code == 202, r.text
    assert ctx.store.get(r.json()["job_id"])["kind"] == "backup.test_remote"
