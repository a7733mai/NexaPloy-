"""Host facts reported by ``GET /v1/server`` and the heartbeat."""

from __future__ import annotations

import os
import platform
import shutil
import socket
from pathlib import Path
from typing import Any

from .. import __version__
from .bench import BenchManager
from .docker import Docker


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


def host_facts(docker: Docker | None, root: Path) -> dict[str, Any]:
    mem = _meminfo()
    osr = _os_release()
    disk_path = root if root.exists() else Path("/")
    du = shutil.disk_usage(disk_path)
    try:
        load = os.getloadavg()
    except OSError:
        load = (0.0, 0.0, 0.0)
    return {
        "hostname": socket.gethostname(),
        "os": osr.get("PRETTY_NAME") or platform.platform(),
        "os_id": osr.get("ID"),
        "os_version": osr.get("VERSION_ID"),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count() or 0,
        "load_avg": [round(x, 2) for x in load],
        "mem_total": mem.get("MemTotal", 0),
        "mem_available": mem.get("MemAvailable", 0),
        "disk_path": str(disk_path),
        "disk_total": du.total,
        "disk_free": du.free,
        "docker_version": docker.server_version() if docker else None,
        "compose_version": docker.compose_version() if docker else None,
        "agent_version": __version__,
    }


def server_info(docker: Docker, benches: BenchManager, server_id: str) -> dict[str, Any]:
    facts = host_facts(docker, benches.settings.root)
    facts["server_id"] = server_id
    facts["benches"] = benches.list(with_status=True)
    return facts
