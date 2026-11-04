"""Command runner abstraction and a thin Docker / Compose wrapper.

Every external command goes through a :class:`Runner` as an argument list
(never ``shell=True``) so tests can swap in :class:`FakeRunner`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from .. import validation as v


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    args: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> "Result":
        if not self.ok:
            raise CommandError(self)
        return self


class CommandError(RuntimeError):
    def __init__(self, result: Result, message: str | None = None):
        self.result = result
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
        msg = message or f"command failed (exit {result.returncode}): {' '.join(result.args[:6])}"
        if tail:
            msg += "\n" + "\n".join(tail)
        super().__init__(msg)


class Runner(Protocol):
    def run(
        self,
        args: Sequence[str],
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        timeout: float | None = None,
    ) -> Result: ...


class SubprocessRunner:
    """Real runner: ``subprocess.run`` with captured text output."""

    def run(self, args, cwd=None, env=None, input=None, timeout=None) -> Result:
        argv = [str(a) for a in args]
        full_env = None
        if env:
            full_env = dict(os.environ)
            full_env.update({k: str(val) for k, val in env.items()})
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                env=full_env,
                input=input,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return Result(127, "", f"{argv[0]}: not found ({exc})", argv)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return Result(124, out, err + f"\ntimed out after {timeout}s", argv)
        return Result(proc.returncode, proc.stdout, proc.stderr, argv)


Matcher = Callable[[list[str]], bool] | str | Sequence[str]


@dataclass
class Call:
    args: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None
    input: str | None = None
    timeout: float | None = None

    @property
    def cmdline(self) -> str:
        return " ".join(self.args)


@dataclass
class _Rule:
    matcher: Matcher
    result: Result | Callable[[Call], Result]
    times: int | None = None  # None = unlimited

    def matches(self, args: list[str]) -> bool:
        m = self.matcher
        if callable(m):
            return bool(m(args))
        if isinstance(m, str):
            return m in " ".join(args)
        n = len(m)
        return any(args[i : i + n] == list(m) for i in range(len(args) - n + 1))


class FakeRunner:
    """Test double: records calls, returns scripted results.

    ``on(matcher, ...)`` registers a rule; the most recently added matching rule
    wins. A matcher is a substring of the joined command line, a token
    sub-sequence (list/tuple) or a predicate. ``result`` may be a callable
    receiving the :class:`Call` (to emulate side effects on the filesystem).
    Unmatched commands succeed with empty output.
    """

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._rules: list[_Rule] = []

    def on(
        self,
        matcher: Matcher,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        *,
        result: Callable[[Call], Result | None] | None = None,
        times: int | None = None,
    ) -> "FakeRunner":
        if result is not None:
            def _wrapped(call: Call, _fn=result, _rc=returncode, _o=stdout, _e=stderr) -> Result:
                r = _fn(call)
                return r if isinstance(r, Result) else Result(_rc, _o, _e)

            res: Result | Callable[[Call], Result] = _wrapped
        else:
            res = Result(returncode, stdout, stderr)
        self._rules.append(_Rule(matcher, res, times))
        return self

    def run(self, args, cwd=None, env=None, input=None, timeout=None) -> Result:
        argv = [str(a) for a in args]
        call = Call(argv, str(cwd) if cwd else None, dict(env) if env else None, input, timeout)
        self.calls.append(call)
        for rule in reversed(self._rules):
            if rule.times == 0 or not rule.matches(argv):
                continue
            if rule.times is not None:
                rule.times -= 1
            res = rule.result(call) if callable(rule.result) else rule.result
            return Result(res.returncode, res.stdout, res.stderr, argv)
        return Result(0, "", "", argv)

    # ---- assertions helpers ---------------------------------------------------
    def cmdlines(self) -> list[str]:
        return [c.cmdline for c in self.calls]

    def find(self, needle: str) -> list[Call]:
        return [c for c in self.calls if needle in c.cmdline]

    def called(self, needle: str) -> bool:
        return bool(self.find(needle))

    def reset(self) -> None:
        self.calls.clear()


class Docker:
    """Docker CLI wrapper bound to the benches directory (§5.4)."""

    def __init__(self, runner: Runner, benches_dir: Path, docker_bin: str = "docker"):
        self.runner = runner
        self.benches_dir = Path(benches_dir)
        self.bin = docker_bin

    # ---- generic ---------------------------------------------------------------
    def run(self, args: Iterable[str], **kw) -> Result:
        return self.runner.run([self.bin, *args], **kw)

    def compose(
        self,
        project: str,
        project_dir: Path,
        *args: str,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        argv = [self.bin, "compose", "-p", project, "--project-directory", str(project_dir), *args]
        return self.runner.run(argv, cwd=project_dir, env=env, input=input, timeout=timeout)

    def bench_compose(self, bench: str, *args: str, **kw) -> Result:
        v.bench_name(bench)
        return self.compose(bench, self.benches_dir / bench, *args, **kw)

    # ---- bench helpers ---------------------------------------------------------
    def running_services(self, bench: str) -> set[str]:
        res = self.bench_compose(bench, "ps", "--status", "running", "--services")
        if not res.ok:
            return set()
        return {line.strip() for line in res.stdout.splitlines() if line.strip()}

    def is_running(self, bench: str, service: str = "backend") -> bool:
        return service in self.running_services(bench)

    def exec_bench(self, bench: str, args: Sequence[str], timeout: float | None = None) -> Result:
        """``docker compose ... exec -T backend bench <args>``."""
        return self.bench_compose(bench, "exec", "-T", "backend", "bench", *args, timeout=timeout)

    def run_bench(self, bench: str, args: Sequence[str], timeout: float | None = None) -> Result:
        """``docker compose ... run --rm backend bench <args>`` (stack down)."""
        return self.bench_compose(bench, "run", "--rm", "-T", "backend", "bench", *args, timeout=timeout)

    def bench_cmd(self, bench: str, args: Sequence[str], timeout: float | None = None) -> Result:
        """Run a ``bench`` command inside the bench: exec if up, else run --rm."""
        if self.is_running(bench):
            return self.exec_bench(bench, args, timeout=timeout)
        return self.run_bench(bench, args, timeout=timeout)

    def ps(self, bench: str) -> list[dict]:
        """``compose ps --all --format json`` parsed (array or NDJSON)."""
        res = self.bench_compose(bench, "ps", "--all", "--format", "json")
        if not res.ok:
            return []
        return parse_json_lines(res.stdout)

    # ---- images / containers ---------------------------------------------------
    def run_rm(self, image: str, args: Sequence[str], *, volumes: Sequence[str] = (),
               user: str | None = None, entrypoint: str | None = None,
               network: str | None = None, timeout: float | None = None) -> Result:
        argv = ["run", "--rm"]
        for vol in volumes:
            argv += ["-v", vol]
        if user:
            argv += ["--user", user]
        if entrypoint:
            argv += ["--entrypoint", entrypoint]
        if network:
            argv += ["--network", network]
        argv += [image, *args]
        return self.run(argv, timeout=timeout)

    def image_exists(self, tag: str) -> bool:
        return self.run(["image", "inspect", "--format", "{{.Id}}", tag]).ok

    def pull(self, tag: str, timeout: float | None = 1800) -> Result:
        return self.run(["pull", tag], timeout=timeout)

    def network_exists(self, name: str) -> bool:
        return self.run(["network", "inspect", "--format", "{{.Name}}", name]).ok

    def ensure_network(self, name: str, internal: bool = False) -> bool:
        """Create the network if missing; return True when created."""
        if self.network_exists(name):
            return False
        args = ["network", "create", "--driver", "bridge"]
        if internal:
            args.append("--internal")
        res = self.run([*args, name])
        if not res.ok and not self.network_exists(name):  # lost a race -> fine
            res.check()
        return True

    def volume_exists(self, name: str) -> bool:
        return self.run(["volume", "inspect", "--format", "{{.Name}}", name]).ok

    def remove_volume(self, name: str) -> Result:
        return self.run(["volume", "rm", "-f", name])

    def server_version(self) -> str | None:
        res = self.run(["version", "--format", "{{.Server.Version}}"], timeout=15)
        return res.stdout.strip() or None if res.ok else None

    def compose_version(self) -> str | None:
        res = self.run(["compose", "version", "--short"], timeout=15)
        return res.stdout.strip() or None if res.ok else None


def parse_json_lines(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


_WS = re.compile(r"\s+")


def parse_app_list(text: str) -> list[str]:
    """Parse ``bench --site s list-apps`` text output into app names."""
    apps: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-", "=")):
            continue
        token = _WS.split(line, 1)[0]
        if v.APP_RE.match(token) and token not in apps:
            apps.append(token)
    return apps


# ---- output capture -------------------------------------------------------------
import contextvars  # noqa: E402

_sink: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "nexaploy_output_sink", default=None
)


class LoggingRunner:
    """Delegating runner that tees each command + output to the active sink.

    Jobs install a sink (the current step's output) with :meth:`capture`; all
    commands issued by runtime managers during that step are then recorded.
    """

    def __init__(self, inner: Runner):
        self.inner = inner

    def run(self, args, cwd=None, env=None, input=None, timeout=None) -> Result:
        sink = _sink.get()
        if sink:
            sink("$ " + " ".join(str(a) for a in args) + "\n")
        res = self.inner.run(args, cwd=cwd, env=env, input=input, timeout=timeout)
        if sink:
            if res.stdout:
                sink(res.stdout if res.stdout.endswith("\n") else res.stdout + "\n")
            if res.stderr:
                sink(res.stderr if res.stderr.endswith("\n") else res.stderr + "\n")
            if not res.ok:
                sink(f"[exit {res.returncode}]\n")
        return res

    @staticmethod
    def capture(sink: Callable[[str], None] | None):
        return _sink.set(sink)

    @staticmethod
    def release(token) -> None:
        _sink.reset(token)
