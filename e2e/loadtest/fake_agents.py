#!/usr/bin/env python3
"""Load test: N fake agents speaking the real agent protocol to a control plane.

Each fake agent polls every --poll seconds and sends a realistic heartbeat (facts, health,
os_updates, 2 benches x 3 sites) every --heartbeat seconds, with random start offsets, like
real agents. Reports requests/s, latency p50/p95/p99 and errors per endpoint.

  python3 e2e/loadtest/fake_agents.py --url http://127.0.0.1:8090 --site-name cp.example.com \
      --tokens tokens.json --servers 300 --duration 180

Needs httpx (the agent venv has it). Use seed.py to create the servers first.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time

import httpx

API = "/api/method/nexaploy.api.agent."


def facts(i: int) -> dict:
	total = 8 * 1024 ** 3
	return {
		"hostname": f"lt-{i:05d}", "os": "Ubuntu 24.04.1 LTS", "os_id": "ubuntu", "os_version": "24.04",
		"kernel": "6.8.0-49-generic", "arch": "x86_64", "cpu_count": 4,
		"load_avg": [round(random.uniform(0.1, 3), 2)] * 3, "mem_total": total,
		"mem_available": int(total * random.uniform(0.3, 0.8)), "mem_used": int(total * 0.4),
		"disk_path": "/opt/nexaploy", "disk_total": 160 * 1024 ** 3, "disk_free": int(160 * 1024 ** 3 * random.uniform(0.2, 0.7)),
		"disk_used": 80 * 1024 ** 3, "mounts": [], "utc_offset_minutes": 0, "timezone": "UTC",
		"docker_version": "27.3.1", "compose_version": "2.29.7", "agent_version": "0.1.0", "public_ips": [f"203.0.113.{i % 250}"],
		"containers": {"total": 20, "running": 20, "unhealthy": 0, "restarting": 0},
		"os_updates": {"supported": True, "pending": 3, "security": 1, "running_kernel": "6.8.0-49-generic",
		               "newest_kernel": "6.8.0-49-generic", "reboot_required": False, "boot_time": "2026-11-01T00:00:00Z",
		               "unattended_security": True, "checked_at": "2026-11-04T00:00:00Z"},
		"health": {"filesystems": [{"mount": "/", "used_percent": 50, "inode_percent": 10}], "memory_percent": 40,
		           "swap_percent": 0, "failed_units": [], "time_synced": True, "clock": time.time()},
	}


def benches(i: int) -> list:
	return [{"name": f"erp-{b}", "image": "nexaploy/bench:v16", "status": "running",
	         "sites": [{"name": f"s{j}.lt-{i:05d}.example.com", "domains": []} for j in range(3)]}
	        for b in range(2)]


class Stats:
	def __init__(self):
		self.lat: dict[str, list[float]] = {}
		self.err: dict[str, int] = {}
		self.codes: dict[str, int] = {}

	def add(self, ep: str, seconds: float, ok: bool, code: str):
		self.lat.setdefault(ep, []).append(seconds)
		if not ok:
			self.err[ep] = self.err.get(ep, 0) + 1
			self.codes[code] = self.codes.get(code, 0) + 1


async def agent(i, cred, args, client, stats, stop):
	headers = {"Authorization": f"token {cred['token']}", "X-Frappe-Site-Name": args.site_name, "Accept": "application/json"}
	sid = cred["server_id"]
	next_poll = time.monotonic() + random.uniform(0, args.poll)
	next_hb = time.monotonic() + random.uniform(0, args.heartbeat)
	while time.monotonic() < stop:
		now = time.monotonic()
		if now >= next_hb:
			ep, req = "heartbeat", client.post(args.url + API + "heartbeat", headers=headers,
			                                   json={"server_id": sid, "facts": facts(i), "benches": benches(i), "agent_version": "0.1.0"})
			next_hb = now + args.heartbeat
		elif now >= next_poll:
			ep, req = "poll", client.get(args.url + API + "poll", headers=headers, params={"server_id": sid})
			next_poll = now + args.poll
		else:
			await asyncio.sleep(min(next_poll, next_hb) - now)
			continue
		t0 = time.monotonic()
		try:
			r = await req
			ok, code = r.status_code == 200, str(r.status_code)
		except Exception as exc:  # timeouts, resets
			ok, code = False, type(exc).__name__
		stats.add(ep, time.monotonic() - t0, ok, code)


def pct(values, p):
	values = sorted(values)
	return values[min(len(values) - 1, int(len(values) * p))] if values else 0


async def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--url", required=True)
	ap.add_argument("--site-name", required=True)
	ap.add_argument("--tokens", required=True)
	ap.add_argument("--servers", type=int, default=100)
	ap.add_argument("--duration", type=int, default=180)
	ap.add_argument("--poll", type=float, default=10)
	ap.add_argument("--heartbeat", type=float, default=60)
	ap.add_argument("--timeout", type=float, default=30)
	args = ap.parse_args()
	creds = json.load(open(args.tokens))[: args.servers]
	stats = Stats()
	limits = httpx.Limits(max_connections=args.servers, max_keepalive_connections=args.servers)
	async with httpx.AsyncClient(timeout=args.timeout, limits=limits, trust_env=False) as client:
		stop = time.monotonic() + args.duration
		await asyncio.gather(*(agent(i, c, args, client, stats, stop) for i, c in enumerate(creds)))
	report = {"servers": len(creds), "duration_s": args.duration, "poll_s": args.poll, "heartbeat_s": args.heartbeat, "endpoints": {}}
	for ep, lat in stats.lat.items():
		report["endpoints"][ep] = {
			"requests": len(lat), "rps": round(len(lat) / args.duration, 1), "errors": stats.err.get(ep, 0),
			"p50_ms": round(statistics.median(lat) * 1000), "p95_ms": round(pct(lat, 0.95) * 1000),
			"p99_ms": round(pct(lat, 0.99) * 1000), "max_ms": round(max(lat) * 1000),
		}
	report["error_codes"] = stats.codes
	print(json.dumps(report, indent=2))


if __name__ == "__main__":
	asyncio.run(main())
