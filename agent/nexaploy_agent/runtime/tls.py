"""TLS health: which certificate does Traefik serve for each routed host?

A cheap probe per host: TCP connect to the local Traefik (``127.0.0.1:<HTTPS_PORT>``,
``HTTPS_PORT`` from ``proxy/.env``, default 443) with SNI = host, read the leaf
certificate and report ``notAfter``, issuer, SANs and whether it is Traefik's
self-signed default certificate (served while the ACME order for the host has not
succeeded: DNS not pointing here, port 80 closed, Let's Encrypt rate limit...).

* verified first (system CA store + hostname check) -> ``trusted: true``;
* when verification fails the certificate is fetched again without verification and
  decoded locally, so the default / expired / staging certificates are still described.

Used by the ``site.tls_status`` job and (throttled, see :class:`HeartbeatTls`) the heartbeat.
Nothing here raises: every host gets a dict, failures in ``error``.
"""

from __future__ import annotations

import os
import socket
import ssl
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config import read_env_file

TRAEFIK_DEFAULT_CN = "TRAEFIK DEFAULT CERT"
DEFAULT_ADDRESS = "127.0.0.1"
DEFAULT_PORT = 443
TIMEOUT = 5.0
MAX_WORKERS = 8
# Heartbeats carry TLS status at most this often (agent-side throttle).
HEARTBEAT_TLS_SECONDS = 10 * 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def https_port(proxy_dir: Path) -> int:
    """Host port Traefik's websecure entrypoint is published on (proxy/.env HTTPS_PORT)."""
    raw = read_env_file(Path(proxy_dir) / ".env").get("HTTPS_PORT", "")
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


# --------------------------------------------------------------------------- parsing (pure)
def _name_dict(rdns: Any) -> dict[str, str]:
    """getpeercert() subject/issuer (tuple of RDN tuples) -> {attr: value}."""
    out: dict[str, str] = {}
    for rdn in rdns or ():
        for pair in rdn or ():
            if isinstance(pair, (tuple, list)) and len(pair) == 2:
                out.setdefault(str(pair[0]), str(pair[1]))
    return out


def _cert_time(value: Any) -> str | None:
    if not value:
        return None
    try:
        return iso(datetime.fromtimestamp(ssl.cert_time_to_seconds(str(value)), timezone.utc))
    except (ValueError, OverflowError):
        return None


def describe_cert(cert: dict[str, Any], host: str) -> dict[str, Any]:
    """Decoded certificate (getpeercert() format) -> the reported fields."""
    subject = _name_dict(cert.get("subject"))
    issuer = _name_dict(cert.get("issuer"))
    sans = [str(v) for k, v in (cert.get("subjectAltName") or ()) if k == "DNS"]
    subject_cn = subject.get("commonName") or ""
    issuer_cn = issuer.get("commonName") or ""
    issuer_org = issuer.get("organizationName") or ""
    default = TRAEFIK_DEFAULT_CN in (subject_cn, issuer_cn)
    return {
        "subject_cn": subject_cn or None,
        "issuer": (f"{issuer_org} ({issuer_cn})" if issuer_org and issuer_cn else issuer_org or issuer_cn) or None,
        "issuer_cn": issuer_cn or None,
        "issuer_org": issuer_org or None,
        "not_before": _cert_time(cert.get("notBefore")),
        "not_after": _cert_time(cert.get("notAfter")),
        "sans": sans,
        "default_cert": default,
        "self_signed": bool(subject) and subject == issuer,
        "matches_host": host_matches(host, sans or ([subject_cn] if subject_cn else [])),
    }


def host_matches(host: str, names: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    for name in names:
        name = str(name).lower().rstrip(".")
        if name == host:
            return True
        if name.startswith("*.") and "." in host and host.split(".", 1)[1] == name[2:]:
            return True
    return False


def _decode_der(der: bytes) -> dict[str, Any]:
    """DER -> getpeercert()-style dict without third-party libraries (CPython's own decoder)."""
    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decode is None or not der:
        return {}
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(ssl.DER_cert_to_PEM_cert(der))
        return decode(path) or {}
    except Exception:
        return {}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- probing
def _connect(host: str, address: str, port: int, timeout: float, context: ssl.SSLContext):
    sock = socket.create_connection((address, port), timeout=timeout)
    try:
        tls = context.wrap_socket(sock, server_hostname=host)
    except Exception:
        sock.close()
        raise
    return tls


def fetch_certificate(host: str, address: str = DEFAULT_ADDRESS, port: int = DEFAULT_PORT,
                      timeout: float = TIMEOUT) -> dict[str, Any]:
    """Probe one host. Returns {host, checked_at, trusted, verify_error, error, ...describe_cert}."""
    out: dict[str, Any] = {"host": host, "checked_at": iso(utcnow()), "trusted": False,
                           "verify_error": None, "error": None}
    verified = ssl.create_default_context()
    try:
        with _connect(host, address, port, timeout, verified) as tls:
            cert = tls.getpeercert() or {}
        out.update(describe_cert(cert, host))
        out["trusted"] = True
        return out
    except ssl.SSLCertVerificationError as exc:
        out["verify_error"] = (getattr(exc, "verify_message", None) or str(exc))[:300]
    except ssl.SSLError as exc:
        out["verify_error"] = str(exc)[:300]
    except OSError as exc:  # refused / timeout / unreachable: nothing more to learn
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return out
    # Not trusted: fetch the certificate without verification to describe it.
    loose = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    loose.check_hostname = False
    loose.verify_mode = ssl.CERT_NONE
    try:
        with _connect(host, address, port, timeout, loose) as tls:
            der = tls.getpeercert(binary_form=True) or b""
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return out
    cert = _decode_der(der)
    if cert:
        out.update(describe_cert(cert, host))
    else:
        out["error"] = "could not decode the served certificate"
    return out


class TlsChecker:
    """Probe every routed host of this server (or a subset) in parallel."""

    def __init__(self, settings, sites, fetch: Callable[..., dict[str, Any]] = fetch_certificate,
                 address: str = DEFAULT_ADDRESS, timeout: float = TIMEOUT):
        self.settings = settings
        self.sites = sites
        self.fetch = fetch
        self.address = address
        self.timeout = timeout

    def hosts(self, site: str | None = None) -> dict[str, str]:
        """{host: owning site} for every routed host (primary names + custom domains)."""
        hosts = self.sites.all_hosts()
        if site:
            hosts = {h: s for h, s in hosts.items() if s == site}
        return hosts

    def check(self, hosts: dict[str, str] | None = None) -> dict[str, Any]:
        hosts = self.hosts() if hosts is None else hosts
        port = https_port(self.settings.proxy_dir)

        def probe(host: str) -> dict[str, Any]:
            try:
                info = self.fetch(host, address=self.address, port=port, timeout=self.timeout)
            except Exception as exc:  # never let one host break the report
                info = {"host": host, "checked_at": iso(utcnow()), "trusted": False,
                        "verify_error": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
            info["site"] = hosts[host]
            return info

        names = sorted(hosts)
        results: dict[str, Any] = {}
        if names:
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(names))) as pool:
                for info in pool.map(probe, names):
                    results[info["host"]] = info
        return {"collected_at": iso(utcnow()), "port": port, "hosts": results}


class HeartbeatTls:
    """Agent-side throttle: the TLS report rides along with a heartbeat at most every 10 minutes."""

    def __init__(self, checker_factory, interval: float = HEARTBEAT_TLS_SECONDS,
                 clock=lambda: utcnow().timestamp()):
        self.checker_factory = checker_factory
        self.interval = interval
        self.clock = clock
        self.last: float | None = None

    def due(self) -> bool:
        return self.last is None or self.clock() - self.last >= self.interval

    def maybe_collect(self) -> dict[str, Any] | None:
        if not self.due():
            return None
        now = self.clock()
        self.last = now
        try:
            report = self.checker_factory().check()
        except Exception:
            return None  # never break the heartbeat; retry next interval
        return report if report.get("hosts") else None
