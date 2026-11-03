"""Container registry credentials (``docker login --password-stdin``).

The control plane sends ``registry: {url, username, password}`` with ``image.build``
(push) and ``bench.create`` / ``bench.update`` (pull). The password is only ever
written to the ``docker login`` process' stdin, never to argv, logs or the job store
(``password`` is a sensitive key, see ``redact.py``).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator

from .docker import Docker, Result

# ghcr.io/org, registry.example.com:5000, registry.example.com/team/sub, docker.io/user
REGISTRY_URL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*$"
)
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,254}$")


def normalise_registry_url(url: str) -> str:
    url = (url or "").strip()
    for scheme in ("https://", "http://"):
        if url.lower().startswith(scheme):
            url = url[len(scheme):]
    url = url.strip("/").lower()
    if not REGISTRY_URL_RE.match(url):
        raise ValueError(f"invalid registry url: {url!r}")
    return url


def registry_host(url: str) -> str | None:
    """Host[:port] to ``docker login`` to; ``None`` means Docker Hub.

    Same rule as Docker: the first path component is a registry host only when it
    contains a ``.`` or ``:`` or is ``localhost``.
    """
    first = normalise_registry_url(url).split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return first
    return None


class Registry(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    url: str
    username: str = ""
    password: str = ""

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return normalise_registry_url(value)

    @field_validator("username")
    @classmethod
    def _user(cls, value: str) -> str:
        if value and not USERNAME_RE.match(value):
            raise ValueError("invalid registry username")
        return value

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        if any(c in value for c in "\r\n\0"):
            raise ValueError("invalid registry password")
        return value

    @property
    def host(self) -> str | None:
        return registry_host(self.url)

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    def covers(self, image: str) -> bool:
        """True when ``image`` lives under this registry url."""
        return image.startswith(self.url + "/")


def docker_login(docker: Docker, registry: Registry, timeout: float = 120) -> Result:
    """``docker login [host] --username U --password-stdin`` (password on stdin only)."""
    args = ["login"]
    if registry.host:
        args.append(registry.host)
    args += ["--username", registry.username, "--password-stdin"]
    return docker.run(args, input=registry.password + "\n", timeout=timeout)
