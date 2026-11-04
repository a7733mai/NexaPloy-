import json

import pytest
from fastapi.testclient import TestClient

from nexaploy_agent import __version__
from nexaploy_agent.api import create_app
from nexaploy_agent.auth import bearer_token, token_matches
from nexaploy_agent.redact import MASK

from .conftest import IMAGE, TOKEN, make_bench, make_site


@pytest.fixture
def client(ctx):
    c = TestClient(create_app(ctx))
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def test_auth_helpers():
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer   abc ") == "abc"
    assert bearer_token("token abc") is None and bearer_token(None) is None
    assert token_matches("abc", "abc") and not token_matches("abc", "abd")
    assert not token_matches("", "") and not token_matches("", "x")


def test_auth_required(ctx):
    c = TestClient(create_app(ctx))
    assert c.get("/v1/ping").status_code == 401
    assert c.get("/v1/ping", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/v1/ping", headers={"Authorization": f"token {TOKEN}"}).status_code == 401
    assert c.post("/v1/benches", json={"name": "abc", "image": IMAGE},
                  headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.get("/v1/ping", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": __version__, "server_id": "srv-test"}


def test_empty_token_rejects_everything(ctx):
    ctx.settings.agent.token = ""
    c = TestClient(create_app(ctx))
    assert c.get("/v1/ping", headers={"Authorization": "Bearer "}).status_code == 401


def test_bench_endpoints(client, ctx, runner):
    r = client.post("/v1/benches", json={"name": "erp-v15", "image": IMAGE})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    job = client.get(f"/v1/jobs/{job_id}").json()
    assert job["kind"] == "bench.create" and job["status"] == "queued" and job["origin"] == "local"
    assert [s["name"] for s in job["steps"]] == ["prepare", "ensure_image", "configure", "start"]

    assert client.post("/v1/benches", json={"name": "Bad Name", "image": IMAGE}).status_code == 422
    assert client.post("/v1/benches/nope-bench/start").status_code == 404
    assert client.get("/v1/benches/nope-bench").status_code == 404
    assert client.get("/v1/benches/BAD!").status_code == 422

    make_bench(ctx, "erp-v15")
    assert client.post("/v1/benches", json={"name": "erp-v15", "image": IMAGE}).status_code == 409
    benches = client.get("/v1/benches").json()
    assert benches[0]["name"] == "erp-v15" and benches[0]["image"] == IMAGE
    for action in ("start", "stop", "restart"):
        r = client.post(f"/v1/benches/erp-v15/{action}")
        assert r.status_code == 202 and r.json()["kind"] == f"bench.{action}"
    r = client.post("/v1/benches/erp-v15/update", json={"image": IMAGE})
    assert r.status_code == 202
    make_site(ctx, "erp-v15", "a.example.com")
    assert client.delete("/v1/benches/erp-v15").status_code == 409
    assert client.delete("/v1/benches/erp-v15?force=true").status_code == 202
    assert client.get("/v1/benches/erp-v15/sites").json() == [
        {"name": "a.example.com", "bench": "erp-v15", "domains": []}]


def test_site_endpoints_and_redaction(client, ctx):
    make_bench(ctx, "erp-v15")
    r = client.post("/v1/benches/erp-v15/sites",
                    json={"name": "erp.example.com", "admin_password": "Sup3rSecret", "apps": ["erpnext"]})
    assert r.status_code == 202
    job = client.get(f"/v1/jobs/{r.json()['job_id']}").json()
    assert job["params"]["admin_password"] == MASK and job["params"]["bench"] == "erp-v15"
    listing = client.get("/v1/jobs").text
    assert "Sup3rSecret" not in listing
    # bad password -> 422 without echoing it
    r = client.post("/v1/benches/erp-v15/sites", json={"name": "x.example.com", "admin_password": "abc"})
    assert r.status_code == 422 and "abc\"" not in r.text

    make_site(ctx, "erp-v15", "a.example.com")
    s = "a.example.com"
    cases = [
        ("post", f"/v1/sites/{s}/apps", {"app": "hrms"}, "site.install_app"),
        ("delete", f"/v1/sites/{s}/apps/hrms", None, "site.uninstall_app"),
        ("post", f"/v1/sites/{s}/domains", {"domain": "www.example.com"}, "site.add_domain"),
        ("delete", f"/v1/sites/{s}/domains/www.example.com", None, "site.remove_domain"),
        ("post", f"/v1/sites/{s}/backup", {"with_files": True, "upload": True}, "site.backup"),
        ("post", f"/v1/sites/{s}/backup", None, "site.backup"),
        ("post", f"/v1/sites/{s}/migrate", None, "site.migrate"),
        ("post", f"/v1/sites/{s}/config", {"key": "maintenance_mode", "value": 1}, "site.set_config"),
        ("post", f"/v1/sites/{s}/restore", {"remote_path": "srv/erp-v15/a.example.com/20261103T120000Z"},
         "site.restore"),
        ("delete", f"/v1/sites/{s}?skip_backup=true", None, "site.drop"),
    ]
    for method, path, body, kind in cases:
        kw = {"json": body} if body is not None else {}
        r = getattr(client, method)(path, **kw)
        assert r.status_code == 202, (path, r.text)
        job = ctx.store.get(r.json()["job_id"])
        assert job["kind"] == kind and job["params"]["site"] == s
    assert ctx.store.get(r.json()["job_id"])["params"]["skip_backup"] is True

    assert client.post("/v1/sites/nope.example.com/migrate").status_code == 404
    assert client.post(f"/v1/sites/{s}/restore", json={}).status_code == 422
    assert client.post(f"/v1/sites/{s}/restore", json={"backup_id": "20261103T120000Z"}).status_code == 404
    assert client.get("/v1/sites").json()[0]["name"] == s
    assert client.get(f"/v1/sites/{s}/backups").json() == {"local": [], "remote": []}
    assert client.get("/v1/jobs/doesnotexist").status_code == 404


def test_job_cancel(client, ctx):
    make_bench(ctx, "erp-v15")
    job_id = client.post("/v1/benches/erp-v15/start").json()["job_id"]
    r = client.post(f"/v1/jobs/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert client.post(f"/v1/jobs/{job_id}/cancel").status_code == 409


def test_image_build_endpoint(client):
    body = {"major": 15, "tag": IMAGE, "apps_json": [{"url": "https://github.com/frappe/erpnext",
                                                      "branch": "version-15"}]}
    assert client.post("/v1/images/build", json=body).status_code == 202
    assert client.post("/v1/images/build", json={**body, "major": 12}).status_code == 422
    jobs = client.get("/v1/jobs", params={"kind": "image.build"}).json()
    assert len(jobs) == 1 and json.dumps(jobs[0]["params"]["apps_json"])


def test_server_facts(client, runner):
    runner.on(["version", "--format"], stdout="27.3.1\n")
    facts = client.get("/v1/server").json()
    assert facts["server_id"] == "srv-test" and facts["docker_version"] == "27.3.1"
    assert facts["cpu_count"] >= 1 and "benches" in facts
