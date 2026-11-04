#!/usr/bin/env python3
"""nexaploy-probe: an external uptime probe worker (pure Python 3.9+ standard library).

Run it on any small VPS in another region than your servers so uptime checks really come from
*outside* (ARCHITECTURE.md §16). It is stateless: every minute it pulls its check list from the
control plane, runs the checks that are due and posts the results back.

    # 1. platform admin: Desk > Uptime Probe > New (name e.g. "fra-1", region "Frankfurt");
    #    the token is shown once (or call nexaploy.api.monitoring_probe.rotate_token)
    # 2. on the VPS:
    curl -fsSL -o nexaploy-probe https://<control-plane>/api/method/nexaploy.api.monitoring_probe.download_script
    NEXAPLOY_URL=https://<control-plane> NEXAPLOY_PROBE_TOKEN=<token> python3 nexaploy-probe
    #    (--once runs a single round; see --help; a systemd unit is in the docstring of main())

The control plane imports `perform_check` from this file for its built-in probe, so both run
the very same check: one HTTPS request, no redirects followed, TLS verified (certificate expiry
captured), optional keyword match in the first 1 MiB of the body.

This module MUST NOT import frappe (it also runs outside the control plane).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

VERSION = "1"
USER_AGENT = f"Nexaploy-Uptime-Probe/{VERSION} (+https://nexaploy.cloud)"
TOKEN_HEADER = "X-Nexaploy-Probe-Token"
BODY_LIMIT = 1024 * 1024
CHECKS_PATH = "/api/method/nexaploy.api.monitoring_probe.checks"
RESULTS_PATH = "/api/method/nexaploy.api.monitoring_probe.results"


def _now_iso():
	return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cert_expiry(sock):
	try:
		cert = sock.getpeercert()
		not_after = cert.get("notAfter") if cert else None
		if not not_after:
			return None
		ts = ssl.cert_time_to_seconds(not_after)
		return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
	except Exception:
		return None


def _status_ok(code, expected):
	"""Expected status: '200', '200-299', '2xx', comma separated (same as monitor_utils)."""
	text = str(expected or "200-399").strip().lower()
	for part in text.replace(" ", ",").split(","):
		part = part.strip()
		if not part:
			continue
		if len(part) == 3 and part.endswith("xx") and part[0].isdigit():
			lo = int(part[0]) * 100
			if lo <= code <= lo + 99:
				return True
			continue
		lo, _, hi = part.partition("-")
		try:
			if int(lo) <= code <= int(hi or lo):
				return True
		except ValueError:
			continue
	return False


def perform_check(check, timeout=None):
	"""Run one check {monitor, url, method, expected_status, keyword, timeout}.

	Returns {monitor, checked_at, ok, status_code, response_ms, error, tls_expires_on}."""
	url = str(check.get("url") or "")
	method = str(check.get("method") or "GET").upper()
	if method not in ("GET", "HEAD"):
		method = "GET"
	timeout = float(timeout or check.get("timeout") or 10)
	keyword = str(check.get("keyword") or "")
	result = {
		"monitor": check.get("monitor"),
		"checked_at": _now_iso(),
		"ok": False,
		"status_code": None,
		"response_ms": None,
		"error": "",
		"tls_expires_on": None,
	}
	parts = urlsplit(url)
	if parts.scheme not in ("https", "http") or not parts.hostname:
		result["error"] = "Invalid URL"
		return result
	path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
	start = time.monotonic()
	conn = None
	try:
		if parts.scheme == "https":
			ctx = ssl.create_default_context()
			conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout, context=ctx)
		else:
			conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
		conn.connect()
		if parts.scheme == "https":
			result["tls_expires_on"] = _cert_expiry(conn.sock)
		conn.request(method, path, headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"})
		resp = conn.getresponse()
		body = b""
		if method == "GET" and keyword:
			body = resp.read(BODY_LIMIT)
		else:
			resp.read(64 * 1024)
		result["response_ms"] = int((time.monotonic() - start) * 1000)
		result["status_code"] = resp.status
		if not _status_ok(resp.status, check.get("expected_status")):
			result["error"] = f"Unexpected status {resp.status}"
		elif keyword and keyword not in body.decode("utf-8", "replace"):
			result["error"] = "Keyword not found"
		else:
			result["ok"] = True
	except ssl.SSLCertVerificationError as e:
		result["error"] = f"TLS certificate error: {getattr(e, 'verify_message', '') or e}"[:300]
	except ssl.SSLError as e:
		result["error"] = f"TLS error: {e}"[:300]
	except (socket.timeout, TimeoutError):
		result["error"] = f"Timeout after {int(timeout)}s"
	except socket.gaierror:
		result["error"] = "DNS lookup failed"
	except ConnectionRefusedError:
		result["error"] = "Connection refused"
	except (OSError, http.client.HTTPException) as e:
		result["error"] = f"Connection error: {type(e).__name__}"
	finally:
		if conn is not None:
			try:
				conn.close()
			except Exception:
				pass
	if result["response_ms"] is None and not result["ok"]:
		result["response_ms"] = int((time.monotonic() - start) * 1000)
	return result


def run_checks(checks, workers=16):
	checks = list(checks)
	if not checks:
		return []
	with ThreadPoolExecutor(max_workers=max(1, min(workers, len(checks)))) as pool:
		return list(pool.map(perform_check, checks))


# --------------------------------------------------------------------------- worker loop


class Client:
	def __init__(self, base_url, token, timeout=20):
		self.base = base_url.rstrip("/")
		self.token = token
		self.timeout = timeout

	def _request(self, path, payload=None):
		data = json.dumps(payload).encode() if payload is not None else None
		req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
		req.add_header(TOKEN_HEADER, self.token)
		req.add_header("User-Agent", USER_AGENT)
		req.add_header("Accept", "application/json")
		if data is not None:
			req.add_header("Content-Type", "application/json")
		with urllib.request.urlopen(req, timeout=self.timeout) as resp:
			body = json.loads(resp.read().decode() or "{}")
		return body.get("message", body)

	def checks(self):
		return self._request(CHECKS_PATH)

	def results(self, results):
		return self._request(RESULTS_PATH, {"results": results})


def due_checks(checks, last_run, now):
	"""Checks whose interval elapsed since this worker last ran them (in-memory state only)."""
	out = []
	for c in checks:
		interval = max(60, int(c.get("interval") or 5) * 60)
		if now - last_run.get(c["monitor"], 0) >= interval - 15:
			out.append(c)
	return out


def main(argv=None):
	"""Worker loop. systemd unit example (/etc/systemd/system/nexaploy-probe.service):

	    [Service]
	    Environment=NEXAPLOY_URL=https://cp.example.com NEXAPLOY_PROBE_TOKEN=...
	    ExecStart=/usr/bin/python3 /opt/nexaploy-probe
	    Restart=always
	    DynamicUser=yes
	"""
	ap = argparse.ArgumentParser(prog="nexaploy-probe", description="Nexaploy external uptime probe")
	ap.add_argument("--url", default=os.environ.get("NEXAPLOY_URL"), help="control plane URL (NEXAPLOY_URL)")
	ap.add_argument("--token", default=os.environ.get("NEXAPLOY_PROBE_TOKEN"), help="probe token (NEXAPLOY_PROBE_TOKEN)")
	ap.add_argument("--workers", type=int, default=int(os.environ.get("NEXAPLOY_PROBE_WORKERS", "16")))
	ap.add_argument("--once", action="store_true", help="run every check once and exit")
	args = ap.parse_args(argv)
	if not args.url or not args.token:
		ap.error("--url and --token (or NEXAPLOY_URL / NEXAPLOY_PROBE_TOKEN) are required")
	client = Client(args.url, args.token)
	last_run = {}
	checks, poll, fetched_at = [], 60, 0.0
	while True:
		now = time.monotonic()
		try:
			if args.once or now - fetched_at >= poll or not fetched_at:
				data = client.checks()
				checks = data.get("checks") or []
				poll = max(30, int(data.get("poll_seconds") or 60))
				fetched_at = now
			todo = checks if args.once else due_checks(checks, last_run, now)
			if todo:
				for c in todo:
					last_run[c["monitor"]] = now
				results = run_checks(todo, args.workers)
				client.results(results)
				print(f"{_now_iso()} posted {len(results)} results ({sum(1 for r in results if not r['ok'])} failing)", flush=True)
		except (urllib.error.URLError, OSError, ValueError) as e:
			print(f"{_now_iso()} control plane unreachable: {e}", file=sys.stderr, flush=True)
		if args.once:
			return 0
		time.sleep(10)


if __name__ == "__main__":
	sys.exit(main())
