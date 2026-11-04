"""``site.configure_email``: point a site's outgoing mail at the platform SMTP relay.

Params: ``{site, enabled, account_name?, email_id, smtp_server, smtp_port, tls_mode
(starttls|ssl|none), username?, password?, verify_smtp?}``. The password reaches the bench only
through the script's stdin (see runtime.mail_relay); it is redacted from params/logs like every
``password`` key (§5.3). The script is fixed code (not user code), so unlike ``site.console`` it
also runs on benches shared with other sites.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..runtime import mail_relay as rm
from .base import SiteParams
from .observability_jobs import _ObsJob

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
HOST_RE = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}$")


class SiteConfigureEmailParams(SiteParams):
    enabled: bool = True
    account_name: str = rm.DEFAULT_ACCOUNT_NAME
    email_id: str | None = None
    smtp_server: str | None = None
    smtp_port: int = Field(587, ge=1, le=65535)
    tls_mode: Literal["starttls", "ssl", "none"] = "starttls"
    username: str | None = Field(None, max_length=320)
    password: str | None = Field(None, max_length=4096)
    verify_smtp: bool = True
    timeout: int = Field(rm.DEFAULT_TIMEOUT, ge=10, le=600)

    @field_validator("account_name")
    @classmethod
    def _account(cls, value: str) -> str:
        if not ACCOUNT_RE.match(value or ""):
            raise ValueError("invalid account name")
        return value

    @field_validator("email_id")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        local, _, domain = value.partition("@")
        value = f"{local}@{domain.lower()}"
        if not EMAIL_RE.match(value):
            raise ValueError("invalid sender e-mail address")
        return value

    @field_validator("smtp_server")
    @classmethod
    def _host(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if len(value) > 253 or not HOST_RE.match(value):
            raise ValueError("invalid SMTP host")
        return value.lower()

    @field_validator("username", "password")
    @classmethod
    def _no_control(cls, value: str | None) -> str | None:
        if value is not None and any(ord(c) < 32 for c in value):
            raise ValueError("control characters are not allowed")
        return value or None

    @model_validator(mode="after")
    def _required(self) -> "SiteConfigureEmailParams":
        if self.enabled and (not self.email_id or not self.smtp_server):
            raise ValueError("email_id and smtp_server are required when enabled")
        if self.enabled and self.username and not self.password:
            raise ValueError("password is required with a username")
        return self


class SiteConfigureEmail(_ObsJob):
    """Result: ``{ok, account, email_id, created, changed: [fields], enabled}``."""

    kind = "site.configure_email"
    steps = ("configure",)
    Params = SiteConfigureEmailParams

    def step_configure(self) -> None:
        bench = self.bench_for_site()
        p = self.p
        self.secret(p.password)
        if not self.ctx.docker.is_running(bench):
            self.fail(f"bench {bench} is not running")
        argv = [self.ctx.docker.bin, "compose", "-p", bench, "--project-directory",
                str(self.ctx.benches.path(bench)), "exec", "-T", "-w", rm.CONTAINER_SITES, "backend",
                *rm.bootstrap_args(self.site)]
        payload = rm.stdin_payload(p.model_dump())
        res = self.quiet(argv, cwd=self.ctx.benches.path(bench), input=payload, timeout=p.timeout)
        data = rm.parse_output(res.stdout)
        if data is None:
            tail = (res.stderr or res.stdout or "").strip()[-2000:]
            self.fail(f"e-mail setup did not report a result (exit {res.returncode}): {tail}")
        self.result.update({
            "ok": bool(data.get("ok")),
            "account": data.get("account") or p.account_name,
            "email_id": data.get("email_id") or p.email_id,
            "created": bool(data.get("created")),
            "changed": [str(c) for c in data.get("changed") or []],
            "enabled": p.enabled,
        })
        if not data.get("ok"):
            if data.get("traceback"):
                self.log(str(data["traceback"]))
            self.fail(f"configuring the Email Account failed: {data.get('error')}")
        changed = self.result["changed"]
        self.log(f"Email Account {self.result['account']!r} "
                 f"{'created' if self.result['created'] else ('updated: ' + ', '.join(changed) if changed else 'unchanged')}"
                 f" ({'outgoing enabled' if p.enabled else 'outgoing disabled'})")


EMAIL_JOBS = (SiteConfigureEmail,)
__all__ = ["EMAIL_JOBS", "SiteConfigureEmail", "SiteConfigureEmailParams"]
