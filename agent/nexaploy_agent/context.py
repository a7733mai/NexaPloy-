"""Wiring: one object that owns settings, the runner, managers and the store."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .config import Settings
from .jobs.store import JobStore
from .runtime.backup import BackupManager
from .runtime.bench import BenchManager
from .runtime.docker import Docker, LoggingRunner, Runner, SubprocessRunner
from .runtime.image import ImageBuilder
from .runtime.proxy import ProxyManager
from .runtime.site import SiteManager
from .runtime.tls import TlsChecker
from .runtime.usage import UsageCollector


class AgentContext:
    def __init__(self, settings: Settings, runner: Runner | None = None,
                 store: JobStore | None = None, templates: Path | None = None):
        self.settings = settings
        self.base_runner = runner or SubprocessRunner()
        self.runner = LoggingRunner(self.base_runner)
        self.docker = Docker(self.runner, settings.benches_dir)
        self.benches = BenchManager(settings, self.docker, templates)
        self.sites = SiteManager(self.benches, self.docker)
        self.backups = BackupManager(settings, self.runner)
        self.proxy = ProxyManager(settings.proxy_sites_dir, templates)
        self.images = ImageBuilder(settings, self.runner)
        self.usage = UsageCollector(settings, self.runner, self.benches, self.sites)
        self.tls = TlsChecker(settings, self.sites)
        self.store = store or JobStore(settings.jobs_db)
        # Set when a job is enqueued in this process so the worker wakes early.
        self.wake = threading.Event()

    def enqueue(self, kind: str, params: dict[str, Any], origin: str = "local",
                remote_id: str | None = None, precheck: bool = False) -> dict[str, Any]:
        from .jobs.registry import enqueue

        job = enqueue(self, kind, params, origin=origin, remote_id=remote_id, precheck=precheck)
        self.wake.set()
        return job
