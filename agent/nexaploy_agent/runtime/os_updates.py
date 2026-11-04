"""Operating-system updates (apt: Ubuntu / Debian).

``OsUpdates.status()`` is reported in every heartbeat as ``facts["os_updates"]``:

    {supported, pending, security, packages: [first 50 names], running_kernel, newest_kernel,
     kernel_update_pending, reboot_required, reboot_required_pkgs, boot_time,
     unattended_security, checked_at}

The package counts come from ``apt list --upgradable`` (uses the package lists as they are:
``apt-daily.timer`` or an ``server.os_update`` job refreshes them) and are cached for 6 hours
in ``<data_dir>/os_updates.json``; update jobs invalidate the cache. Reboot / kernel / boot
time / unattended settings are cheap file reads and are refreshed on every call.

Other distributions report ``{"supported": false}``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write

CACHE_TTL_SECONDS = 6 * 3600
CACHE_FILE = "os_updates.json"
MAX_PACKAGES = 50
APT_ENV = {"LANG": "C", "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "a"}
DPKG_LOCK_TIMEOUT = 600

AUTO_UPGRADES_FILE = "20auto-upgrades"
NEXAPLOY_UNATTENDED_FILE = "52nexaploy-unattended"


@dataclass
class OsPaths:
    """Host paths (overridable in tests)."""

    os_release: Path = Path("/etc/os-release")
    boot_dir: Path = Path("/boot")
    reboot_required: Path = Path("/var/run/reboot-required")
    reboot_required_pkgs: Path = Path("/var/run/reboot-required.pkgs")
    proc_stat: Path = Path("/proc/stat")
    apt_conf_dir: Path = Path("/etc/apt/apt.conf.d")


def utc_iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- parsing
_UPGRADABLE_RE = re.compile(r"^(?P<name>[A-Za-z0-9.+:_-]+)/(?P<suite>\S+)\s+(?P<version>\S+)\s+(?P<arch>\S+)")


def parse_upgradable(text: str) -> list[dict[str, Any]]:
    """``apt list --upgradable`` -> [{name, suite, version, security}].

    Example line: ``openssl/jammy-updates,jammy-security 3.0.2-0ubuntu1.15 amd64 [upgradable
    from: 3.0.2-0ubuntu1.14]``. A package is a security update when one of its suites is a
    security suite (``jammy-security``, ``bookworm-security``, ``stable-security``...).
    """
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("Listing", "WARNING", "N:", "W:", "E:")):
            continue
        m = _UPGRADABLE_RE.match(line)
        if not m:
            continue
        suites = m.group("suite").split(",")
        out.append({
            "name": m.group("name"),
            "suite": m.group("suite"),
            "version": m.group("version"),
            "security": any("security" in s.lower() for s in suites),
        })
    return out


def natural_key(text: str) -> tuple:
    """'5.15.0-101-generic' sorts after '5.15.0-91-generic' (numbers compared as numbers)."""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                 for part in re.findall(r"\d+|[A-Za-z]+", text or ""))


def kernel_versions(boot_dir: Path) -> list[str]:
    try:
        names = [p.name for p in boot_dir.iterdir() if p.name.startswith("vmlinuz-")]
    except OSError:
        return []
    return sorted((n[len("vmlinuz-"):] for n in names if len(n) > len("vmlinuz-")), key=natural_key)


def newest_kernel(boot_dir: Path) -> str | None:
    versions = kernel_versions(boot_dir)
    return versions[-1] if versions else None


def read_reboot_required(flag: Path, pkgs: Path) -> tuple[bool, list[str]]:
    if not flag.exists():
        return False, []
    try:
        names = [line.strip() for line in pkgs.read_text().splitlines() if line.strip()]
    except OSError:
        names = []
    return True, sorted(dict.fromkeys(names))


def boot_time(proc_stat: Path) -> int | None:
    """Unix time of the last boot (``btime`` in /proc/stat)."""
    try:
        for line in proc_stat.read_text().splitlines():
            if line.startswith("btime "):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


_APT_SETTING_RE = re.compile(r'^\s*(?P<key>[A-Za-z0-9:_-]+)\s+"(?P<value>[^"]*)"\s*;', re.M)


def parse_apt_conf(text: str) -> dict[str, str]:
    return {m.group("key"): m.group("value") for m in _APT_SETTING_RE.finditer(text or "")}


def unattended_enabled(apt_conf_dir: Path) -> bool:
    """APT::Periodic::Unattended-Upgrade "1" in 20auto-upgrades (later files override)."""
    values: dict[str, str] = {}
    try:
        files = sorted(p for p in apt_conf_dir.iterdir() if p.is_file())
    except OSError:
        return False
    for path in files:
        try:
            values.update(parse_apt_conf(path.read_text(errors="replace")))
        except OSError:
            continue
    return values.get("APT::Periodic::Unattended-Upgrade", "0").strip() not in ("", "0")


def auto_upgrades_content(enabled: bool) -> str:
    flag = "1" if enabled else "0"
    return (
        "// Managed by Nexaploy (server.os_configure)\n"
        f'APT::Periodic::Update-Package-Lists "{flag}";\n'
        f'APT::Periodic::Unattended-Upgrade "{flag}";\n'
        'APT::Periodic::AutocleanInterval "7";\n'
    )


def unattended_content() -> str:
    # Security origins only; reboots are scheduled by Nexaploy (maintenance window), never
    # by unattended-upgrades itself.
    return (
        "// Managed by Nexaploy (server.os_configure)\n"
        "Unattended-Upgrade::Allowed-Origins {\n"
        '\t"${distro_id}:${distro_codename}-security";\n'
        '\t"${distro_id}ESMApps:${distro_codename}-apps-security";\n'
        '\t"${distro_id}ESM:${distro_codename}-infra-security";\n'
        "};\n"
        "Unattended-Upgrade::Origins-Pattern {\n"
        '\t"origin=Debian,codename=${distro_codename}-security,label=Debian-Security";\n'
        '\t"origin=Debian,codename=${distro_codename},label=Debian-Security";\n'
        "};\n"
        'Unattended-Upgrade::Automatic-Reboot "false";\n'
        'Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";\n'
        'Unattended-Upgrade::Remove-Unused-Dependencies "true";\n'
        'Dpkg::Options { "--force-confdef"; "--force-confold"; };\n'
    )


def os_release(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line:
                k, _, val = line.partition("=")
                out[k.strip()] = val.strip().strip('"')
    except OSError:
        pass
    return out


def is_apt_distro(osr: dict[str, str]) -> bool:
    ids = {osr.get("ID", "").lower(), *osr.get("ID_LIKE", "").lower().split()}
    return bool(ids & {"debian", "ubuntu"})


def apt_get(*args: str) -> list[str]:
    return ["apt-get", "-o", f"DPkg::Lock::Timeout={DPKG_LOCK_TIMEOUT}", *args]


def upgrade_command(mode: str) -> list[str]:
    if mode == "security":
        return ["unattended-upgrade", "-v"]
    return apt_get("-y", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
                   "full-upgrade")


def reboot_command(job_id: str, delay_seconds: int) -> list[str]:
    """Transient timer rebooting the host after the job reported success (same pattern as the
    agent restart in runtime/upgrade.py: the reboot runs outside the worker's cgroup)."""
    suffix = re.sub(r"[^a-z0-9]", "", (job_id or "").lower())[:26] or str(int(time.time()))
    return [
        "systemd-run", "--quiet", "--collect",
        f"--unit=nexaploy-os-reboot-{suffix}",
        f"--on-active={int(delay_seconds)}s",
        "--timer-property=AccuracySec=1s",
        "systemctl", "reboot",
    ]


SHUTDOWN_FALLBACK = ["shutdown", "-r", "+1", "Nexaploy: reboot after system updates"]


# --------------------------------------------------------------------------- service
class OsUpdates:
    def __init__(self, data_dir: Path, runner, paths: OsPaths | None = None,
                 ttl: float = CACHE_TTL_SECONDS, clock=time.time, which=shutil.which):
        self.data_dir = Path(data_dir)
        self.which = which
        self.runner = runner
        self.paths = paths or OsPaths()
        self.ttl = ttl
        self.clock = clock

    @property
    def cache_path(self) -> Path:
        return self.data_dir / CACHE_FILE

    def supported(self) -> bool:
        return is_apt_distro(os_release(self.paths.os_release)) and self.which("apt-get") is not None

    # ---- cache ------------------------------------------------------------------------
    def _read_cache(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or self.clock() - float(data.get("_ts") or 0) > self.ttl:
            return None
        return data

    def invalidate(self) -> None:
        try:
            self.cache_path.unlink()
        except OSError:
            pass

    def _collect_packages(self) -> dict[str, Any]:
        res = self.runner.run(["apt", "list", "--upgradable"], env=APT_ENV, timeout=120)
        if not res.ok:
            return {"error": (res.stderr or res.stdout or "apt list failed").strip()[-300:]}
        pkgs = parse_upgradable(res.stdout)
        return {
            "pending": len(pkgs),
            "security": sum(1 for p in pkgs if p["security"]),
            "packages": [p["name"] for p in pkgs][:MAX_PACKAGES],
            "security_packages": [p["name"] for p in pkgs if p["security"]][:MAX_PACKAGES],
            "checked_at": utc_iso(self.clock()),
        }

    def running_kernel(self) -> str | None:
        return os.uname().release if hasattr(os, "uname") else None

    # ---- status ------------------------------------------------------------------------
    def live(self) -> dict[str, Any]:
        """The cheap part (file reads only)."""
        p = self.paths
        running = self.running_kernel()
        newest = newest_kernel(p.boot_dir)
        reboot, pkgs = read_reboot_required(p.reboot_required, p.reboot_required_pkgs)
        kernel_pending = bool(newest and running and natural_key(newest) > natural_key(running))
        return {
            "running_kernel": running,
            "newest_kernel": newest,
            "kernel_update_pending": kernel_pending,
            "reboot_required": reboot or kernel_pending,
            "reboot_required_pkgs": pkgs[:MAX_PACKAGES],
            "boot_time": boot_time(p.proc_stat),
            "unattended_security": unattended_enabled(p.apt_conf_dir),
        }

    def status(self, force: bool = False) -> dict[str, Any]:
        """Never raises (heartbeat)."""
        try:
            if not self.supported():
                return {"supported": False}
            cached = None if force else self._read_cache()
            if cached is None:
                cached = self._collect_packages()
                cached["_ts"] = self.clock()
                try:
                    atomic_write(self.cache_path, json.dumps(cached) + "\n", mode=0o600)
                except OSError:
                    pass
            out = {"supported": True, **{k: v for k, v in cached.items() if k != "_ts"}}
            out.update(self.live())
            return out
        except Exception as exc:  # pragma: no cover - defensive
            return {"supported": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
