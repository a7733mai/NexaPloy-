"""Agent configuration (ARCHITECTURE.md §5.1).

Loaded from ``/opt/nexaploy/config/agent.toml``; the ``NEXAPLOY_CONFIG``
environment variable overrides the path. A missing file yields defaults, which
is convenient for tests and first boot (the API refuses every request while
``agent.token`` is empty).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_PATH = Path("/opt/nexaploy/config/agent.toml")
CONFIG_ENV = "NEXAPLOY_CONFIG"

# Fixed UIDs/GIDs (ARCHITECTURE.md §2).
TRAEFIK_UID = 3100
MARIADB_UID = 3101
REDIS_UID = 3102
FRAPPE_UID = 3113


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AgentSection(_Section):
    server_id: str = ""
    token: str = ""
    bind: str = "127.0.0.1"
    port: int = 8787
    root: Path = Path("/opt/nexaploy")


class ControlPlaneSection(_Section):
    url: str = ""
    token: str = ""
    # All-in-one installs reach the control plane bench on 127.0.0.1:<port>; nginx picks
    # the Frappe site from X-Frappe-Site-Name, so the agent sends it when this is set.
    site_name: str = ""
    heartbeat_seconds: int = Field(default=60, ge=5)
    poll_seconds: int = Field(default=10, ge=1)
    timeout_seconds: float = 15.0

    @property
    def enabled(self) -> bool:
        return bool(self.url.strip())


class BackupSection(_Section):
    remote: str = "nexaploy-backups"
    prefix: str = ""
    retention_days: int = Field(default=14, ge=0)
    keep_min: int = Field(default=3, ge=1)


class BenchDefaults(_Section):
    """Defaults for a new bench's .env (§3.1). Optional ``[bench]`` section."""

    tz: str = "UTC"
    db_host: str = "nexaploy-db"
    db_port: int = 3306
    backend_workers: int = 2
    backend_cpus: str = "2"
    backend_mem: str = "4G"
    worker_cpus: str = "1"
    worker_mem: str = "1G"


class ImagesSection(_Section):
    """Optional ``[images]`` section; paths default to ``<root>/images/...``."""

    build_script: Path | None = None
    context: Path | None = None
    build_timeout_seconds: int = 2 * 3600


class Settings(_Section):
    agent: AgentSection = AgentSection()
    control_plane: ControlPlaneSection = ControlPlaneSection()
    backup: BackupSection = BackupSection()
    bench: BenchDefaults = BenchDefaults()
    images: ImagesSection = ImagesSection()
    # Path the settings were loaded from (not part of the file).
    source: Path | None = Field(default=None, exclude=True)

    # ---- derived paths (§2) -------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(self.agent.root)

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def benches_dir(self) -> Path:
        return self.root / "benches"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def proxy_dir(self) -> Path:
        return self.root / "proxy"

    @property
    def proxy_sites_dir(self) -> Path:
        return self.proxy_dir / "config" / "sites"

    @property
    def images_dir(self) -> Path:
        return self.root / "images"

    @property
    def builds_dir(self) -> Path:
        return self.images_dir / "builds"

    @property
    def build_script(self) -> Path:
        return self.images.build_script or (self.images_dir / "build.sh")

    @property
    def build_context(self) -> Path:
        return self.images.context or (self.images_dir / "frappe_docker")

    @property
    def db_env(self) -> Path:
        return self.config_dir / "db.env"

    @property
    def rclone_conf(self) -> Path:
        return self.config_dir / "rclone.conf"

    @property
    def rclone_state(self) -> Path:
        """sha256 of the OAuth tokens we last wrote into rclone.conf (see
        runtime.backup.plan_backup_remote). Kept outside rclone.conf: rclone rewrites it."""
        return self.config_dir / "rclone.state.json"

    @property
    def data_dir(self) -> Path:
        return self.root / "agent" / "data"

    @property
    def jobs_db(self) -> Path:
        return self.data_dir / "jobs.db"

    @property
    def api_url(self) -> str:
        host = self.agent.bind
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.agent.port}"

    @property
    def backup_prefix(self) -> str:
        return (self.backup.prefix or self.agent.server_id or "default").strip("/")

    # ---- secrets ------------------------------------------------------------
    def db_root_password(self) -> str:
        """Read ``DB_ROOT_PASSWORD`` from ``config/db.env``."""
        values = read_env_file(self.db_env)
        pw = values.get("DB_ROOT_PASSWORD") or values.get("MARIADB_ROOT_PASSWORD")
        if not pw:
            raise ConfigError(f"DB_ROOT_PASSWORD missing in {self.db_env}")
        return pw

    def rclone_remote_configured(self) -> bool:
        try:
            text = self.rclone_conf.read_text()
        except OSError:
            return False
        return f"[{self.backup.remote}]" in text


class ConfigError(RuntimeError):
    pass


def config_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get(CONFIG_ENV)
    return Path(env) if env else DEFAULT_CONFIG_PATH


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    p = config_path(path)
    data: dict[str, Any] = {}
    if p.exists():
        try:
            with p.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {p}: {exc}") from exc
    settings = Settings.model_validate(data)
    settings.source = p
    return settings


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file (comments, optional quotes)."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def dump_toml(data: dict[str, dict[str, Any]]) -> str:
    """Serialise a two-level ``{section: {key: scalar}}`` mapping to TOML."""
    out: list[str] = []
    for section, values in data.items():
        out.append(f"[{section}]")
        for key, value in values.items():
            if value is None:
                continue
            out.append(f"{key} = {_toml_value(value)}")
        out.append("")
    return "\n".join(out)


def update_config_file(path: Path, updates: dict[str, dict[str, Any]]) -> None:
    """Merge ``updates`` into the TOML file at ``path`` (mode 600)."""
    data: dict[str, dict[str, Any]] = {}
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    for section, values in updates.items():
        data.setdefault(section, {}).update(values)
    from .fsutil import atomic_write

    atomic_write(path, dump_toml(data), mode=0o600)
