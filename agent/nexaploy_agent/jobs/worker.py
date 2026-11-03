"""Single worker loop: one job at a time, resumable (§5.3).

* On start, jobs left ``running`` by a crash are re-queued; their successful
  steps are skipped on the next run (steps are idempotent).
* The oldest queued job is claimed atomically and its steps run in order.
* The first failing step fails the job; the remaining steps are ``skipped``.
* ``on_change(job_id)`` is invoked after every job/step status change (the
  control-plane reporter uses it to push ``job_update``).
* Once a job is final, secret params are scrubbed from the store.
"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable

from pydantic import ValidationError as PydanticValidationError

from ..redact import mask_text, redact_params, collect_secrets
from ..validation import ValidationError
from ..runtime.docker import CommandError
from .base import StepFailed
from .registry import get_job_class

log = logging.getLogger("nexaploy.worker")

OnChange = Callable[[str], None]


class Worker:
    def __init__(self, ctx, on_change: OnChange | None = None, idle_seconds: float = 2.0):
        self.ctx = ctx
        self.store = ctx.store
        self.on_change = on_change
        self.idle_seconds = idle_seconds

    def _notify(self, job_id: str) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(job_id)
        except Exception:  # never let reporting break job execution
            log.exception("on_change callback failed for %s", job_id)

    def recover(self) -> list[str]:
        ids = self.store.reset_stale()
        for job_id in ids:
            log.warning("re-queued interrupted job %s", job_id)
            self._notify(job_id)
        return ids

    def run_once(self) -> bool:
        record = self.store.claim_next()
        if record is None:
            return False
        self.execute(record)
        return True

    def run_forever(self, stop: threading.Event) -> None:
        self.recover()
        log.info("worker started")
        while not stop.is_set():
            try:
                busy = self.run_once()
            except Exception:
                log.exception("worker loop error")
                busy = False
            if not busy:
                self.ctx.wake.wait(self.idle_seconds)
                self.ctx.wake.clear()
        log.info("worker stopped")

    # ------------------------------------------------------------------------------
    def _finish(self, record: dict[str, Any], status: str, error: str | None) -> None:
        job_id = record["id"]
        self.store.finish(job_id, status, error)
        self.store.replace_params(job_id, redact_params(record["params"]))
        log.info("job %s %s %s%s", job_id, record["kind"], status, f": {error}" if error else "")
        self._notify(job_id)

    def execute(self, record: dict[str, Any]) -> None:
        job_id = record["id"]
        self._notify(job_id)
        secrets = collect_secrets(record["params"])
        try:
            cls = get_job_class(record["kind"])
            job = cls(self.ctx, record)
        except (KeyError, PydanticValidationError, ValidationError) as exc:
            self.store.skip_remaining(job_id, -1)
            self._finish(record, "failure", mask_text(f"invalid job: {exc}", secrets))
            return

        steps = record.get("steps") or self.store.steps(job_id)
        for step in steps:
            idx, name = step["idx"], step["name"]
            if step["status"] == "success":
                continue  # resumed job: already done
            self.store.start_step(job_id, idx)
            self._notify(job_id)
            try:
                job.run_step(idx, name)
            except Exception as exc:
                if isinstance(exc, (StepFailed, ValidationError, CommandError)):
                    error = job.mask(str(exc))
                else:
                    error = job.mask(f"{type(exc).__name__}: {exc}")
                    self.store.append_output(job_id, idx, job.mask(traceback.format_exc()))
                self.store.append_output(job_id, idx, f"ERROR: {error}\n")
                self.store.end_step(job_id, idx, "failure")
                self.store.skip_remaining(job_id, idx)
                self._finish(record, "failure", error)
                return
            self.store.end_step(job_id, idx, "success")
            self._notify(job_id)
        self._finish(record, "success", None)
