"""cloud-init user data for a freshly created Hosted server (pure: no frappe import).

The VM boots, cloud-init runs install.sh (ARCHITECTURE.md §5.7) as root with a single-use
registration token; install.sh registers with the control plane, which attaches the agent
to the pre-created Server (Server Registration Token.server, see api/agent.register).
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Sequence

INSTALL_LOG = "/var/log/nexaploy-install.log"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~/-]*)?$")
EMAIL_RE = re.compile(r"^[^@\s'\"]+@[^@\s'\"]+\.[^@\s'\"]+$")
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def install_command(control_plane: str, registration_token: str, acme_email: str | None = None) -> str:
	"""`curl -fsSL <cp>/install.sh | bash -s -- --token T --control-plane <cp> [--acme-email E]`."""
	cp = (control_plane or "").strip().rstrip("/")
	if not URL_RE.match(cp):
		raise ValueError("Control plane URL must be an http(s) URL")
	if not TOKEN_RE.match(registration_token or ""):
		raise ValueError("Invalid registration token")
	parts = [
		f"curl -fsSL {shlex.quote(cp + '/install.sh')} | bash -s --",
		"--token", shlex.quote(registration_token),
		"--control-plane", shlex.quote(cp),
	]
	if acme_email:
		if not EMAIL_RE.match(acme_email):
			raise ValueError("Invalid ACME e-mail")
		parts += ["--acme-email", shlex.quote(acme_email)]
	return " ".join(parts)


def _yaml_str(value: str) -> str:
	# a JSON string is a valid YAML double-quoted scalar
	return json.dumps(value)


def build_cloud_init(
	control_plane: str,
	registration_token: str,
	acme_email: str | None = None,
	hostname: str | None = None,
	ssh_authorized_keys: Sequence[str] | None = None,
) -> str:
	"""#cloud-config that installs the agent and registers it with the control plane.

	install.sh output goes to /var/log/nexaploy-install.log on the VM. The command is retried
	once after 30 s (apt locks right after first boot are common).
	"""
	command = install_command(control_plane, registration_token, acme_email)
	script = f"({command}) >> {INSTALL_LOG} 2>&1 || (sleep 30; ({command}) >> {INSTALL_LOG} 2>&1)"
	lines = ["#cloud-config"]
	if hostname:
		if not HOSTNAME_RE.match(hostname):
			raise ValueError("Invalid hostname")
		lines += [f"hostname: {hostname}", "preserve_hostname: false"]
	lines += [
		"package_update: true",
		"packages:",
		"  - curl",
		"  - ca-certificates",
	]
	keys = [k.strip() for k in (ssh_authorized_keys or []) if k and k.strip()]
	if keys:
		lines.append("ssh_authorized_keys:")
		lines += [f"  - {_yaml_str(k)}" for k in keys]
	lines += [
		"runcmd:",
		f"  - [bash, -c, {_yaml_str(script)}]",
		"final_message: \"nexaploy bootstrap finished after $UPTIME seconds\"",
	]
	return "\n".join(lines) + "\n"
