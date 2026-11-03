import json

import httpx
import pytest

from nexaploy_agent import __version__
from nexaploy_agent.config import Settings, load_settings, update_config_file
from nexaploy_agent.control_plane import (
    ControlPlaneClient, ControlPlaneError, ControlPlaneService, backoff_delays, job_update_payload,
)
from nexaploy_agent.jobs.worker import Worker

from .conftest import make_bench, make_site

CP = "https://cp.example.com"


class Recorder:
    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = request.url.path.rsplit(".", 1)[-1]
        handler = self.routes.get(name)
        if handler is None:
            return httpx.Response(404)
        return handler(request) if callable(handler) else handler

    def bodies(self, name):
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("." + name)]


def make_client(settings, routes, **kw):
    rec = Recorder(routes)
    http = httpx.Client(transport=httpx.MockTransport(rec))
    sleeps: list[float] = []
    client = ControlPlaneClient(settings, client=http, sleep=sleeps.append, **kw)
    return client, rec, sleeps


@pytest.fixture
def cp_settings(settings):
    settings.control_plane.url = CP + "/"
    settings.control_plane.token = "srv-token"
    return settings


def test_noop_when_unconfigured(settings):
    client, rec, _ = make_client(settings, {})
    assert not client.enabled
    assert client.heartbeat({}, []) is None and client.poll() == [] and client.job_update({"id": "x"}) is None
    assert rec.requests == []


def test_heartbeat(cp_settings, ctx):
    client, rec, _ = make_client(cp_settings, {"heartbeat": httpx.Response(200, json={"message": {"ok": True}})})
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com", domains=["www.example.com"])
    ControlPlaneService(ctx, client).heartbeat_once()
    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == f"{CP}/api/method/nexaploy.api.agent.heartbeat"
    assert req.headers["Authorization"] == "token srv-token"
    body = json.loads(req.content)
    assert body["server_id"] == "srv-test" and body["agent_version"] == __version__
    assert body["facts"]["agent_version"] == __version__
    assert body["benches"][0]["name"] == "erp-v15"
    assert body["benches"][0]["sites"] == [{"name": "a.example.com", "bench": "erp-v15",
                                            "domains": ["www.example.com"]}]


def test_poll_enqueues_and_dedupes(cp_settings, ctx):
    make_bench(ctx, "erp-v15")
    items = [
        {"job_id": "AJ-0001", "kind": "bench.restart", "params": {"bench": "erp-v15"}},
        {"job_id": "AJ-0002", "kind": "site.create",
         "params": {"bench": "erp-v15", "name": "n.example.com", "admin_password": "pw123456", "apps": []}},
        {"job_id": "AJ-0003", "kind": "bench.explode", "params": {}},
        {"job_id": "AJ-0004", "kind": "site.create", "params": {"bench": "erp-v15", "name": "BAD",
                                                                 "admin_password": "pw123456"}},
    ]
    client, rec, _ = make_client(cp_settings, {"poll": httpx.Response(200, json={"message": items})})
    svc = ControlPlaneService(ctx, client)
    created = svc.poll_once()
    assert len(created) == 4
    req = rec.requests[0]
    assert req.method == "GET" and req.url.params["server_id"] == "srv-test"
    j1 = ctx.store.find_remote("AJ-0001")
    assert j1["status"] == "queued" and j1["origin"] == "control_plane" and j1["kind"] == "bench.restart"
    assert ctx.store.find_remote("AJ-0002")["params"]["name"] == "n.example.com"
    bad = ctx.store.find_remote("AJ-0003")
    assert bad["status"] == "failure" and "unknown job kind" in bad["error"]
    bad2 = ctx.store.find_remote("AJ-0004")
    assert bad2["status"] == "failure" and "pw123456" not in json.dumps(bad2)
    # second poll returning the same items creates nothing new
    assert svc.poll_once() == []
    assert len(ctx.store.list(limit=100)) == 4


def test_job_update_flow(cp_settings, ctx, runner):
    make_bench(ctx, "erp-v15")
    client, rec, _ = make_client(cp_settings, {"job_update": httpx.Response(200, json={"message": {"ok": True}})})
    svc = ControlPlaneService(ctx, client)
    local = ctx.enqueue("bench.stop", {"bench": "erp-v15"})  # local jobs are not reported
    remote = ctx.enqueue("bench.restart", {"bench": "erp-v15"}, origin="control_plane", remote_id="AJ-9")
    w = Worker(ctx, on_change=svc.notify)
    w.run_once()
    w.run_once()
    assert svc.flush_updates() == 1  # coalesced: one update per job
    bodies = rec.bodies("job_update")
    assert len(bodies) == 1
    body = bodies[0]
    assert body["job_id"] == "AJ-9" and body["server_id"] == "srv-test" and body["status"] == "success"
    assert body["steps"][0]["name"] == "restart" and body["steps"][0]["status"] == "success"
    assert {"idx", "name", "status", "output", "started_at", "ended_at"} <= set(body["steps"][0])
    assert "error" in body and "result" in body
    assert local["id"] != remote["id"]


def test_job_update_payload_shape():
    job = {"id": "01J", "remote_id": "AJ-1", "status": "failure", "error": "boom",
           "result": {"tag": "x"}, "steps": [{"idx": 0, "name": "a", "status": "failure", "output": None,
                                              "started_at": "t1", "ended_at": "t2"}]}
    p = job_update_payload("srv", job)
    assert p == {"server_id": "srv", "job_id": "AJ-1", "status": "failure", "error": "boom",
                 "result": {"tag": "x"},
                 "steps": [{"idx": 0, "name": "a", "status": "failure", "output": "",
                            "started_at": "t1", "ended_at": "t2"}]}


def test_retry_with_backoff(cp_settings):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"message": []})

    client, rec, sleeps = make_client(cp_settings, {"poll": flaky}, attempts=5, backoff_base=1.0)
    assert client.poll() == []
    assert calls["n"] == 3 and len(sleeps) == 2
    assert 0.8 <= sleeps[0] <= 1.2 and 1.6 <= sleeps[1] <= 2.4

    client, rec, sleeps = make_client(cp_settings, {"poll": httpx.Response(500)}, attempts=3)
    with pytest.raises(ControlPlaneError, match="unreachable"):
        client.poll()
    assert len(rec.requests) == 3


def test_auth_errors_not_retried(cp_settings):
    client, rec, sleeps = make_client(cp_settings, {"poll": httpx.Response(401)})
    with pytest.raises(ControlPlaneError, match="authentication"):
        client.poll()
    assert len(rec.requests) == 1 and sleeps == []


def test_backoff_delays():
    assert backoff_delays(5, 1, 60) == [1, 2, 4, 8]
    assert backoff_delays(10, 1, 30)[-1] == 30
    assert backoff_delays(1) == []


def test_register_and_config_update(settings, tmp_path):
    settings.control_plane.url = CP
    client, rec, _ = make_client(settings, {
        "register": httpx.Response(200, json={"message": {"server_id": "srv-new1", "server_token": "tok-xyz"}}),
    })
    creds = client.register("reg-123", "host1", {"cpu_count": 2})
    assert creds == {"server_id": "srv-new1", "server_token": "tok-xyz"}
    body = json.loads(rec.requests[0].content)
    assert body["registration_token"] == "reg-123" and body["hostname"] == "host1"
    assert body["agent_version"] == __version__

    path = tmp_path / "agent.toml"
    path.write_text('[agent]\ntoken = "local"\nport = 8787\n')
    update_config_file(path, {"agent": {"server_id": "srv-new1"},
                              "control_plane": {"url": CP, "token": "tok-xyz"}})
    s = load_settings(path)
    assert s.agent.server_id == "srv-new1" and s.agent.token == "local" and s.control_plane.token == "tok-xyz"
    assert path.stat().st_mode & 0o777 == 0o600


def test_settings_paths(tmp_path, monkeypatch):
    cfg = tmp_path / "agent.toml"
    cfg.write_text('[agent]\nroot = "/srv/nx"\nbind = "0.0.0.0"\n[backup]\nretention_days = 7\n')
    monkeypatch.setenv("NEXAPLOY_CONFIG", str(cfg))
    s = load_settings()
    assert s.source == cfg and s.backup.retention_days == 7
    assert str(s.benches_dir) == "/srv/nx/benches" and str(s.jobs_db) == "/srv/nx/agent/data/jobs.db"
    assert str(s.proxy_sites_dir) == "/srv/nx/proxy/config/sites"
    assert str(s.rclone_conf) == "/srv/nx/config/rclone.conf" and str(s.db_env) == "/srv/nx/config/db.env"
    assert str(s.build_script) == "/srv/nx/images/build.sh"
    assert s.api_url == "http://127.0.0.1:8787"
    assert Settings().control_plane.enabled is False
