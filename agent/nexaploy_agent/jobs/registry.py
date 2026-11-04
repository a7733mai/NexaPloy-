"""Job kind registry and enqueue helper."""

from __future__ import annotations

from typing import Any

from .base import Job
from .bench_jobs import BenchCreate, BenchRemove, BenchRestart, BenchStart, BenchStop, BenchUpdate
from .image_jobs import ImageBuild
from .server_jobs import BackupPruneRemote, BackupTestRemote, ServerConfigureBackups, ServerHealth
from .site_jobs import (
    SiteAddDomain, SiteBackup, SiteCreate, SiteDrop, SiteInstallApp, SiteMigrate,
    SiteGetConfig, SiteRemoveDomain, SiteRestore, SiteSetConfig, SiteUninstallApp, SiteUpdateConfig,
)
from .usage_jobs import SiteUsage
from .lifecycle_jobs import SiteClone, SiteRename
from .observability_jobs import SiteAnalytics, SiteConsole, SiteLogs, SiteSlowQueries, SiteSql
from .tls_jobs import SiteTlsStatus
from .email_jobs import SiteConfigureEmail
from .smoke_jobs import SiteSmokeCheck
from .pitr_jobs import ServerArchiveBinlogs, SiteRestorePitr
from .agent_jobs import AgentRollback, AgentUpgrade
from .storage_jobs import STORAGE_JOB_CLASSES
from .maintenance_jobs import MAINTENANCE_JOB_CLASSES
from .controlplane_jobs import ControlPlaneUpdate

JOB_CLASSES: tuple[type[Job], ...] = (
    ImageBuild,
    BenchCreate, BenchStart, BenchStop, BenchRestart, BenchUpdate, BenchRemove,
    SiteCreate, SiteInstallApp, SiteUninstallApp, SiteAddDomain, SiteRemoveDomain,
    SiteBackup, SiteRestore, SiteMigrate, SiteSetConfig, SiteDrop,
    ServerConfigureBackups, ServerHealth, BackupPruneRemote, BackupTestRemote,
    SiteUsage, SiteGetConfig, SiteUpdateConfig,
    SiteClone, SiteRename,
    SiteLogs, SiteAnalytics, SiteSlowQueries, SiteSql, SiteConsole,
    SiteTlsStatus,
    SiteConfigureEmail,
    SiteSmokeCheck,
    SiteRestorePitr, ServerArchiveBinlogs,
    AgentUpgrade, AgentRollback,
    *STORAGE_JOB_CLASSES,
    *MAINTENANCE_JOB_CLASSES,
    ControlPlaneUpdate,
)
REGISTRY: dict[str, type[Job]] = {cls.kind: cls for cls in JOB_CLASSES}


class UnknownJobKind(KeyError):
    pass


def get_job_class(kind: str) -> type[Job]:
    try:
        return REGISTRY[kind]
    except KeyError:
        raise UnknownJobKind(f"unknown job kind {kind!r}") from None


def enqueue(ctx, kind: str, params: dict[str, Any], origin: str = "local",
            remote_id: str | None = None, precheck: bool = False) -> dict[str, Any]:
    """Validate params, optionally precheck state, and insert a queued job.

    Raises pydantic ``ValidationError`` / ``validation.ValidationError`` for bad
    params, ``NotFound`` / ``Conflict`` from the precheck, ``UnknownJobKind``.
    """
    cls = get_job_class(kind)
    clean = cls.validate_params(params or {})
    if precheck:
        cls.precheck(ctx, clean)
    return ctx.store.create(kind, clean, cls.step_names(clean), origin=origin, remote_id=remote_id)
