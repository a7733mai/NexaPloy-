"""Outbound control-plane protocol (§5.6).

* heartbeat every ``heartbeat_seconds``: ``{server_id, facts, benches, agent_version[, usage][, tls]}``
  (``usage`` - per-site CPU/DB/storage - at most every 15 minutes, see runtime/usage.py;
  ``tls`` - certificate served for every routed host - at most every 10 minutes, runtime/tls.py)
* poll every ``poll_seconds``: ``{"message": [{job_id, kind, params}]}`` -> enqueued
  locally with ``origin=control_plane, remote_id=job_id`` (deduplicated)
* job_update on every status/step change of control-plane jobs
* register (BYOS, called once by install.sh)

All calls carry ``Authorization: token <control_plane.token>`` and retry with
exponential backoff. Everything is a no-op when ``control_plane.url`` is empty;
the agent keeps working standalone when the control plane is down.
"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from typing import Any, Callable

import httpx

from . import __version__
from .config import Settings

log = logging.getLogger("nexaploy.control_plane")

API = "/api/method/nexaploy.api.agent."


class ControlPlaneError(RuntimeError):
    pass


def backoff_delays(attempts: int, base: float = 1.0, cap: float = 60.0) -> list[float]:
    """Delays between attempts: base * 2**n, capped (jitter added at sleep time)."""
    return [min(cap, base * (2 ** n)) for n in range(max(0, attempts - 1))]


def unwrap(payload: Any) -> Any:
    """Frappe wraps return values in ``{"message": ...}``."""
    if isinstance(payload, dict) and "message" in payload:
        return payload["message"]
    return payload


def job_update_payload(server_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """Map a local job (with steps) to the job_update body."""
    return {
        "server_id": server_id,
        "job_id": job.get("remote_id") or job["id"],
        "status": job["status"],
        "steps": [
            {
                "idx": s["idx"],
                "name": s["name"],
                "status": s["status"],
                "output": s.get("output") or "",
                "started_at": s.get("started_at"),
                "ended_at": s.get("ended_at"),
            }
            for s in job.get("steps", [])
        ],
        "error": job.get("error"),
        "result": job.get("result") or {},
    }


class ControlPlaneClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, attempts: int = 5,
                 backoff_base: float = 1.0, backoff_cap: float = 60.0):
        self.settings = settings
        self.cp = settings.control_plane
        self._client = client
        self.sleep = sleep
        self.attempts = attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap

    @property
    def enabled(self) -> bool:
        return self.cp.enabled

    @property
    def base_url(self) -> str:
        return self.cp.url.rstrip("/")

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.cp.timeout_seconds, follow_redirects=False)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def _headers(self, token: str | None = None) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": f"nexaploy-agent/{__version__}"}
        tok = self.cp.token if token is None else token
        if tok:
            h["Authorization"] = f"token {tok}"
        if self.cp.site_name:
            h["X-Frappe-Site-Name"] = self.cp.site_name
        return h

    def request(self, method: str, name: str, *, json: Any = None, params: dict | None = None,
                token: str | None = None, attempts: int | None = None) -> Any:
        """Call ``nexaploy.api.agent.<name>`` with retries; returns the unwrapped message."""
        url = f"{self.base_url}{API}{name}"
        tries = attempts or self.attempts
        delays = backoff_delays(tries, self.backoff_base, self.backoff_cap)
        last: Exception | None = None
        for attempt in range(tries):
            try:
                resp = self.client.request(method, url, json=json, params=params, headers=self._headers(token))
                if resp.status_code in (401, 403):
                    raise ControlPlaneError(f"{name}: authentication failed ({resp.status_code})")
                if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                    raise ControlPlaneError(f"{name}: HTTP {resp.status_code}: {resp.text[:300]}")
                resp.raise_for_status()
                return unwrap(resp.json()) if resp.content else None
            except ControlPlaneError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                if attempt < len(delays):
                    delay = delays[attempt] * (0.8 + 0.4 * random.random())
                    log.debug("%s failed (%s); retrying in %.1fs", name, exc, delay)
                    self.sleep(delay)
        raise ControlPlaneError(f"{name}: control plane unreachable: {last}")

    # ---- protocol calls ----------------------------------------------------------------
    def heartbeat(self, facts: dict[str, Any], benches: list[dict[str, Any]],
                  usage: dict[str, Any] | None = None, tls: dict[str, Any] | None = None) -> Any:
        if not self.enabled:
            return None
        body: dict[str, Any] = {
            "server_id": self.settings.agent.server_id,
            "facts": facts,
            "benches": benches,
            "agent_version": __version__,
        }
        if usage:
            body["usage"] = usage
        if tls:
            body["tls"] = tls
        return self.request("POST", "heartbeat", json=body)

    def poll(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        data = self.request("GET", "poll", params={"server_id": self.settings.agent.server_id})
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and item.get("job_id") and item.get("kind")]

    def job_update(self, job: dict[str, Any]) -> Any:
        if not self.enabled:
            return None
        return self.request("POST", "job_update", json=job_update_payload(self.settings.agent.server_id, job))

    def register(self, registration_token: str, hostname: str, facts: dict[str, Any]) -> dict[str, str]:
        """BYOS registration -> ``{server_id, server_token}``."""
        if not self.enabled:
            raise ControlPlaneError("control_plane.url is not set")
        data = self.request(
            "POST", "register",
            json={"registration_token": registration_token, "hostname": hostname,
                  "facts": facts, "agent_version": __version__},
            token=registration_token,
        )
        if not isinstance(data, dict) or not data.get("server_id") or not data.get("server_token"):
            raise ControlPlaneError(f"unexpected register response: {data!r}")
        return {"server_id": str(data["server_id"]), "server_token": str(data["server_token"])}


class ControlPlaneService:
    """Background threads for the worker process: heartbeat, poll, reporter."""

    def __init__(self, ctx, client: ControlPlaneClient | None = None):
        self.ctx = ctx
        self.client = client or ControlPlaneClient(ctx.settings)
        self._updates: "queue.Queue[str]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        from .runtime.usage import HeartbeatUsage

        self.usage = HeartbeatUsage(lambda: self.ctx.usage)
        from .runtime.tls import HeartbeatTls

        self.tls = HeartbeatTls(lambda: self.ctx.tls)

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    # on_change hook for the Worker -------------------------------------------------------
    def notify(self, job_id: str) -> None:
        if self.enabled:
            self._updates.put(job_id)

    # single iterations (unit-testable) ------------------------------------------------------
    def heartbeat_once(self) -> None:
        from .runtime.server import host_facts

        facts = host_facts(self.ctx.docker, self.ctx.settings.root)
        # server maintenance (§17): never raise, the heartbeat must go out
        facts["os_updates"] = self.ctx.os_updates.status()
        facts["health"] = self.ctx.health.light()
        benches = self.ctx.benches.list(with_status=True)
        for b in benches:
            b["sites"] = [self.ctx.sites.info(b["name"], s) for s in b.get("sites", [])]
        usage = self.usage.maybe_collect()
        tls = self.tls.maybe_collect()
        try:
            self.client.heartbeat(facts, benches, usage=usage, tls=tls)
        except Exception:
            if usage is not None:
                self.usage.last = None  # not delivered: send it with the next heartbeat
            if tls is not None:
                self.tls.last = None
            raise

    def poll_once(self) -> list[str]:
        created: list[str] = []
        for item in self.client.poll():
            remote_id = str(item["job_id"])
            existing = self.ctx.store.find_remote(remote_id)
            if existing:
                # Already have it (e.g. CP re-sent after a lost response): re-report.
                self.notify(existing["id"])
                continue
            job = self.enqueue_remote(item["kind"], item.get("params") or {}, remote_id)
            created.append(job["id"])
            self.notify(job["id"])
        return created

    def enqueue_remote(self, kind: str, params: dict[str, Any], remote_id: str) -> dict[str, Any]:
        try:
            return self.ctx.enqueue(kind, params, origin="control_plane", remote_id=remote_id)
        except Exception as exc:  # invalid params / unknown kind -> record as failed job
            from .redact import collect_secrets, mask_text, redact_params

            error = mask_text(f"rejected: {exc}", collect_secrets(params))
            log.warning("control plane job %s (%s) rejected: %s", remote_id, kind, error)
            return self.ctx.store.create(
                kind, redact_params(params), [], origin="control_plane",
                remote_id=remote_id, status="failure", error=error,
            )

    def flush_updates(self, block: bool = False, timeout: float = 1.0) -> int:
        """Send one job_update per pending job (coalesced). Returns the count sent."""
        pending: list[str] = []
        try:
            pending.append(self._updates.get(block=block, timeout=timeout if block else None))
            while True:
                pending.append(self._updates.get_nowait())
        except queue.Empty:
            pass
        sent = 0
        for job_id in dict.fromkeys(pending):
            try:
                job = self.ctx.store.get(job_id)
            except KeyError:
                continue
            if job.get("origin") != "control_plane":
                continue
            try:
                self.client.job_update(job)
                sent += 1
            except ControlPlaneError as exc:
                log.warning("job_update %s failed: %s; will retry", job_id, exc)
                self._updates.put(job_id)
                time.sleep(min(30, self.ctx.settings.control_plane.poll_seconds))
        return sent

    # loops ----------------------------------------------------------------------------------
    def _loop(self, stop: threading.Event, interval: float, fn: Callable[[], Any], name: str) -> None:
        while not stop.is_set():
            try:
                fn()
            except Exception as exc:
                log.warning("%s failed: %s", name, exc)
            stop.wait(interval)

    def _reporter(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.flush_updates(block=True, timeout=1.0)
            except Exception:
                log.exception("reporter failed")

    def start(self, stop: threading.Event) -> None:
        if not self.enabled:
            log.info("control plane not configured; running standalone")
            return
        cp = self.ctx.settings.control_plane
        specs = [
            ("cp-heartbeat", lambda: self._loop(stop, cp.heartbeat_seconds, self.heartbeat_once, "heartbeat")),
            ("cp-poll", lambda: self._loop(stop, cp.poll_seconds, self.poll_once, "poll")),
            ("cp-reporter", lambda: self._reporter(stop)),
        ]
        for name, target in specs:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("control plane threads started (%s)", self.client.base_url)

    def join(self, timeout: float = 5.0) -> None:
        for t in self._threads:
            t.join(timeout)
