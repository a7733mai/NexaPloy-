"""Host facts reported by ``GET /v1/server`` and the heartbeat."""

from __future__ import annotations

import ipaddress
import os
import platform
import shutil
import socket
import time
from pathlib import Path
from typing import Any, Iterable

from .. import __version__
from .bench import BenchManager
from .docker import Docker, parse_json_lines

# Shared containers (§2); bench containers are matched by compose project.
SHARED_CONTAINERS = ("nexaploy-db", "nexaploy-traefik", "nexaploy-socket-proxy")
# One-shot compose services that legitimately exit.
ONE_SHOT_SERVICES = frozenset({"configurator", "create-site", "migrator"})


def _meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[key] = int(parts[0]) * 1024  # kB -> bytes
    except OSError:
        pass
    return out


def _os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line:
                k, _, val = line.partition("=")
                out[k] = val.strip().strip('"')
    except OSError:
        pass
    return out


def disk_mounts(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Usage for each existing path (``/`` and the Nexaploy root).

    Both are reported even when they share a filesystem (``device`` tells them apart).
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in paths:
        p = Path(p)
        if str(p) in seen or not p.exists():
            continue
        seen.add(str(p))
        try:
            dev = p.stat().st_dev
            du = shutil.disk_usage(p)
        except OSError:
            continue
        used = du.total - du.free
        out.append({
            "path": str(p), "device": dev, "total": du.total, "used": used, "free": du.free,
            "used_percent": round(100.0 * used / du.total, 1) if du.total else 0.0,
        })
    return out


def _parse_labels(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    for part in str(raw or "").split(","):
        k, sep, val = part.partition("=")
        if sep:
            out[k.strip()] = val.strip()
    return out


def _health(status: str) -> str | None:
    s = (status or "").lower()
    if "(unhealthy)" in s:
        return "unhealthy"
    if "(healthy)" in s:
        return "healthy"
    if "health: starting" in s:
        return "starting"
    return None


def parse_containers(text: str) -> list[dict[str, Any]]:
    """``docker ps --all --format '{{json .}}'`` -> [{name, project, service, state, health, status}]."""
    rows = []
    for d in parse_json_lines(text):
        labels = _parse_labels(d.get("Labels"))
        status = str(d.get("Status") or "")
        state = str(d.get("State") or "").lower()
        if not state:
            state = "running" if status.lower().startswith("up") else "exited"
        rows.append({
            "name": str(d.get("Names") or d.get("Name") or "").split(",")[0].lstrip("/"),
            "project": labels.get("com.docker.compose.project", ""),
            "service": labels.get("com.docker.compose.service", ""),
            "state": state,
            "health": _health(status),
            "status": status,
        })
    return rows


def is_unhealthy(c: dict[str, Any]) -> bool:
    if c.get("service") in ONE_SHOT_SERVICES:
        return False
    return c.get("state") != "running" or c.get("health") == "unhealthy"


def summarize_containers(containers: list[dict[str, Any]], benches: Iterable[str]) -> dict[str, Any]:
    """Keep Nexaploy containers only and aggregate per-bench health."""
    bench_set = set(benches)
    ours = [c for c in containers
            if c["name"] in SHARED_CONTAINERS or c["name"].startswith("nexaploy-") or c["project"] in bench_set]
    bench_health: dict[str, dict[str, Any]] = {}
    for b in sorted(bench_set):
        mine = [c for c in ours if c["project"] == b and c.get("service") not in ONE_SHOT_SERVICES]
        bad = sorted(c["name"] for c in mine if is_unhealthy(c))
        bench_health[b] = {
            "total": len(mine),
            "running": sum(1 for c in mine if c["state"] == "running"),
            "unhealthy": bad,
            "healthy": bool(mine) and not bad,
        }
    return {
        "containers": [{k: c[k] for k in ("name", "project", "service", "state", "health")} for c in ours],
        "bench_health": bench_health,
        "unhealthy_containers": sum(1 for c in ours if is_unhealthy(c)),
    }


def container_facts(docker: Docker, benches: Iterable[str]) -> dict[str, Any]:
    res = docker.run(["ps", "--all", "--no-trunc", "--format", "{{json .}}"], timeout=30)
    containers = parse_containers(res.stdout) if res.ok else []
    out = summarize_containers(containers, benches)
    out["containers_ok"] = res.ok
    return out


def _bench_names(root: Path) -> list[str]:
    from .. import validation as v

    d = root / "benches"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and v.BENCH_RE.match(p.name))


# Public resolvers used only as routing targets: connect() on a UDP socket sends nothing,
# it just makes the kernel pick the source address of the default route.
_ROUTE_PROBES = ((socket.AF_INET, "1.1.1.1"), (socket.AF_INET6, "2606:4700:4700::1111"))


def public_addresses(probes=_ROUTE_PROBES) -> list[str]:
    """Global (public) source addresses of the default IPv4/IPv6 routes.

    Used by the control plane as the DNS target for custom domains (A/AAAA records) when the
    Server has no public_ipv4/public_ipv6. Behind NAT (private source address) nothing is
    reported for that family; the control plane then falls back to the address the agent
    connects from, or to the operator's override.
    """
    found: list[str] = []
    for family, target in probes:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 53))
                addr = sock.getsockname()[0]
        except OSError:
            continue
        try:
            ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
        except ValueError:
            continue
        if ip.is_global and str(ip) not in found:
            found.append(str(ip))
    return found


def host_facts(docker: Docker | None, root: Path) -> dict[str, Any]:
    mem = _meminfo()
    osr = _os_release()
    disk_path = root if root.exists() else Path("/")
    du = shutil.disk_usage(disk_path)
    try:
        load = os.getloadavg()
    except OSError:
        load = (0.0, 0.0, 0.0)
    mem_total = mem.get("MemTotal", 0)
    mem_available = mem.get("MemAvailable", 0)
    facts: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "os": osr.get("PRETTY_NAME") or platform.platform(),
        "os_id": osr.get("ID"),
        "os_version": osr.get("VERSION_ID"),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count() or 0,
        "load_avg": [round(x, 2) for x in load],
        "mem_total": mem_total,
        "mem_available": mem_available,
        "mem_used": max(0, mem_total - mem_available),
        "disk_path": str(disk_path),
        "disk_total": du.total,
        "disk_free": du.free,
        "disk_used": du.total - du.free,
        "mounts": disk_mounts([Path("/"), root]),
        # server-local time zone, used by the control plane for update windows
        "utc_offset_minutes": int(time.localtime().tm_gmtoff // 60),
        "timezone": time.strftime("%Z"),
        "docker_version": docker.server_version() if docker else None,
        "compose_version": docker.compose_version() if docker else None,
        "agent_version": __version__,
        "public_ips": public_addresses(),
    }
    if docker:
        facts.update(container_facts(docker, _bench_names(root)))
    return facts


def server_info(docker: Docker, benches: BenchManager, server_id: str) -> dict[str, Any]:
    facts = host_facts(docker, benches.settings.root)
    facts["server_id"] = server_id
    facts["benches"] = benches.list(with_status=True)
    return facts
