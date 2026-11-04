"""TLS health: certificate probe, site.tls_status job, heartbeat ``tls`` block, public_ips fact."""

import json
import shutil
import socket
import ssl
import subprocess
import threading

import httpx
import pytest

from nexaploy_agent.control_plane import ControlPlaneClient, ControlPlaneService
from nexaploy_agent.runtime import tls as T
from nexaploy_agent.runtime.server import public_addresses

from .conftest import make_bench, make_site, run_job

LE_CERT = {
    "subject": ((("commonName", "erp.example.com"),),),
    "issuer": ((("countryName", "US"),), (("organizationName", "Let's Encrypt"),), (("commonName", "R11"),)),
    "notBefore": "Oct  1 00:00:00 2026 GMT",
    "notAfter": "Dec 30 00:00:00 2026 GMT",
    "subjectAltName": (("DNS", "erp.example.com"),),
}
DEFAULT_CERT = {
    "subject": ((("commonName", "TRAEFIK DEFAULT CERT"),),),
    "issuer": ((("commonName", "TRAEFIK DEFAULT CERT"),),),
    "notBefore": "Nov  1 00:00:00 2026 GMT",
    "notAfter": "Nov  1 00:00:00 2027 GMT",
    "subjectAltName": (("DNS", "a.b.traefik.default"),),
}


def test_describe_cert():
    d = T.describe_cert(LE_CERT, "erp.example.com")
    assert d["issuer"] == "Let's Encrypt (R11)" and d["issuer_org"] == "Let's Encrypt"
    assert d["not_after"] == "2026-12-30T00:00:00Z" and d["not_before"] == "2026-10-01T00:00:00Z"
    assert d["matches_host"] and not d["default_cert"] and not d["self_signed"]
    dd = T.describe_cert(DEFAULT_CERT, "shop.example.org")
    assert dd["default_cert"] and dd["self_signed"] and not dd["matches_host"]
    assert T.describe_cert({}, "x.example.com")["not_after"] is None


def test_host_matches():
    assert T.host_matches("a.example.com", ["*.example.com"])
    assert not T.host_matches("a.b.example.com", ["*.example.com"])
    assert T.host_matches("A.Example.com.", ["a.example.com"])
    assert not T.host_matches("example.com", ["*.example.com"])


def test_https_port(tmp_path):
    assert T.https_port(tmp_path) == 443
    (tmp_path / ".env").write_text("HTTP_PORT=80\nHTTPS_PORT=8443\n")
    assert T.https_port(tmp_path) == 8443
    (tmp_path / ".env").write_text("HTTPS_PORT=nope\n")
    assert T.https_port(tmp_path) == 443


def _fake_fetch(seen):
    def fetch(host, address, port, timeout):
        seen.append((host, address, port))
        if host == "boom.example.com":
            raise RuntimeError("kaput")
        cert = LE_CERT if host == "a.example.com" else DEFAULT_CERT
        return {"host": host, "checked_at": "x", "trusted": host == "a.example.com", "error": None,
                **T.describe_cert(cert, host)}
    return fetch


def test_checker_and_job(ctx, worker):
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com", domains=["shop.example.org", "boom.example.com"])
    make_site(ctx, "erp-v15", "b.example.com")
    (ctx.settings.proxy_dir).mkdir(parents=True, exist_ok=True)
    (ctx.settings.proxy_dir / ".env").write_text("HTTPS_PORT=443\n")
    seen = []
    ctx.tls.fetch = _fake_fetch(seen)

    report = ctx.tls.check()
    assert set(report["hosts"]) == {"a.example.com", "shop.example.org", "boom.example.com", "b.example.com"}
    assert all(addr == "127.0.0.1" and port == 443 for _, addr, port in seen)
    assert report["hosts"]["shop.example.org"]["default_cert"] and report["hosts"]["shop.example.org"]["site"] == "a.example.com"
    assert "kaput" in report["hosts"]["boom.example.com"]["error"]

    job = run_job(ctx, worker, "site.tls_status", {"site": "a.example.com", "domains": ["Shop.Example.org", "gone.example.net"]})
    assert job["status"] == "success", job
    hosts = job["result"]["hosts"]
    assert set(hosts) == {"shop.example.org", "gone.example.net"}
    assert hosts["gone.example.net"]["error"] == "not routed on this server"
    assert "default certificate" in job["steps"][0]["output"]

    job = run_job(ctx, worker, "site.tls_status", {"site": "b.example.com"})
    assert set(job["result"]["hosts"]) == {"b.example.com"}
    job = run_job(ctx, worker, "site.tls_status", {})
    assert len(job["result"]["hosts"]) == 4


def test_job_rejects_unknown_site(ctx):
    from nexaploy_agent.jobs.base import NotFound

    with pytest.raises(NotFound):
        ctx.enqueue("site.tls_status", {"site": "nope.example.com"}, precheck=True)


def test_heartbeat_tls_throttle_and_body(settings, ctx):
    settings.control_plane.url = "https://cp.example.com"
    settings.control_plane.token = "srv-token"
    make_bench(ctx, "erp-v15")
    make_site(ctx, "erp-v15", "a.example.com")
    ctx.tls.fetch = _fake_fetch([])
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"ok": True}})

    client = ControlPlaneClient(settings, client=httpx.Client(transport=httpx.MockTransport(handler)),
                                sleep=lambda s: None)
    svc = ControlPlaneService(ctx, client)
    svc.heartbeat_once()
    svc.heartbeat_once()
    assert bodies[0]["tls"]["hosts"]["a.example.com"]["issuer"] == "Let's Encrypt (R11)"
    assert "tls" not in bodies[1]  # throttled
    assert "public_ips" in bodies[0]["facts"]

    clock = [0.0]
    hb = T.HeartbeatTls(lambda: ctx.tls, clock=lambda: clock[0])
    assert hb.maybe_collect() is not None
    clock[0] += 60
    assert hb.maybe_collect() is None
    clock[0] += T.HEARTBEAT_TLS_SECONDS
    assert hb.maybe_collect() is not None


def test_public_addresses_filters_private():
    class FakeSock:
        def __init__(self, addr):
            self.addr = addr

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def connect(self, target):
            pass

        def getsockname(self):
            return (self.addr, 0)

    import nexaploy_agent.runtime.server as srv

    addrs = iter(["10.0.0.5", "2a01:4f8::1"])
    orig = srv.socket.socket
    srv.socket.socket = lambda *a, **k: FakeSock(next(addrs))
    try:
        assert public_addresses() == ["2a01:4f8::1"]
    finally:
        srv.socket.socket = orig


# --------------------------------------------------------------------------- real probe
@pytest.fixture
def tls_server(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    r = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
         "-days", "30", "-subj", "/CN=TRAEFIK DEFAULT CERT"],
        capture_output=True,
    )
    if r.returncode != 0:
        pytest.skip("openssl cannot create a certificate here")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True) as s:
                    s.recv(1)
            except (OSError, ssl.SSLError):
                pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join(2)
    srv.close()


def test_fetch_certificate_default_cert(tls_server):
    info = T.fetch_certificate("shop.example.org", port=tls_server, timeout=3)
    assert info["error"] is None, info
    assert not info["trusted"] and info["verify_error"]
    assert info["default_cert"] and info["subject_cn"] == "TRAEFIK DEFAULT CERT"
    assert info["not_after"] and info["not_after"].endswith("Z")


def test_fetch_certificate_refused():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    info = T.fetch_certificate("x.example.com", port=port, timeout=1)
    assert info["error"] and not info["trusted"]
