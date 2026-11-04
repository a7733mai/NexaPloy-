"""Job base class (§5.3).

A job declares an ordered list of step names; each name maps to a method
``step_<name>``. Steps must be idempotent (check-then-act): on resume after a
crash, already-successful steps are skipped and the interrupted step re-runs.
State that later steps (or a resumed run) need is kept in ``self.result``,
which is persisted after every step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, field_validator

from .. import validation as v
from ..redact import collect_secrets, mask_text
from ..runtime.docker import CommandError, LoggingRunner, Result

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AgentContext


class StepFailed(RuntimeError):
    """A step could not complete; the message becomes the job error."""


class NotFound(LookupError):
    """Referenced bench/site does not exist (API: 404)."""


class Conflict(RuntimeError):
    """Request conflicts with current state (API: 409)."""


class BaseParams(BaseModel):
    # Unknown keys are ignored so the control plane can add fields without
    # breaking older agents.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class Job:
    kind: ClassVar[str] = ""
    steps: ClassVar[tuple[str, ...]] = ()
    Params: ClassVar[type[BaseModel]] = BaseParams

    def __init__(self, ctx: "AgentContext", record: dict[str, Any]):
        self.ctx = ctx
        self.record = record
        self.id: str = record["id"]
        self.p = self.Params.model_validate(record["params"])
        self.result: dict[str, Any] = dict(record.get("result") or {})
        self._secrets: set[str] = collect_secrets(record["params"])
        self._idx: int | None = None

    # ---- class-level hooks -------------------------------------------------------
    @classmethod
    def validate_params(cls, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalise params (raises pydantic/ValidationError)."""
        return cls.Params.model_validate(raw).model_dump(mode="json")

    @classmethod
    def step_names(cls, params: dict[str, Any]) -> list[str]:
        return list(cls.steps)

    @classmethod
    def precheck(cls, ctx: "AgentContext", params: dict[str, Any]) -> None:
        """Cheap state checks at enqueue time (API -> 404/409). Optional."""

    # ---- helpers for steps ----------------------------------------------------------
    @property
    def settings(self):
        return self.ctx.settings

    def secret(self, value: str | None) -> str | None:
        """Register a value to be masked in output and errors."""
        if value:
            self._secrets.add(value)
        return value

    def mask(self, text: str) -> str:
        return mask_text(text, self._secrets)

    def log(self, message: str) -> None:
        if self._idx is None:
            return
        if not message.endswith("\n"):
            message += "\n"
        self.ctx.store.append_output(self.id, self._idx, self.mask(message))

    def skip(self, reason: str) -> None:
        """Log that the step's effect is already in place (check-then-act)."""
        self.log(f"already done: {reason}")

    def check(self, res: Result, what: str) -> Result:
        if not res.ok:
            raise StepFailed(self.mask(f"{what} failed: {CommandError(res)}"))
        return res

    def fail(self, message: str) -> None:
        raise StepFailed(self.mask(message))

    def save(self) -> None:
        self.ctx.store.set_result(self.id, self.result)

    # ---- execution (driven by the worker) ----------------------------------------------
    def run_step(self, idx: int, name: str) -> None:
        method = getattr(self, f"step_{name}", None)
        if method is None:
            raise StepFailed(f"{self.kind}: unknown step {name!r}")
        self._idx = idx
        token = LoggingRunner.capture(self.log)
        try:
            method()
        finally:
            LoggingRunner.release(token)
            self.save()
            self._idx = None


class BenchParams(BaseParams):
    bench: str

    @field_validator("bench")
    @classmethod
    def _bench(cls, value: str) -> str:
        return v.bench_name(value)


class SiteParams(BaseParams):
    site: str

    @field_validator("site")
    @classmethod
    def _site(cls, value: str) -> str:
        return v.site_name(value)
