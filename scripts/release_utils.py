#!/usr/bin/env python3
"""Release helpers used by scripts/release.sh and .github/workflows/release.yml.

    release_utils.py bump  <repo> <version>          set every version string
    release_utils.py check <repo> <version>          exit 1 unless all of them equal <version>
    release_utils.py changelog <file> <version> <date>
        "## [<version>] - Unreleased" -> "## [<version>] - <date>", or rename
        "## [Unreleased]" to the version (a new empty Unreleased section stays on top)
    release_utils.py notes <file> <version>          print that version's CHANGELOG section

Stdlib only (runs on a bare GitHub runner).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")

# (path relative to the repo, regex with one group around the version)
VERSION_FILES = (
	("agent/nexaploy_agent/__init__.py", r'^__version__ = "([^"]+)"'),
	("agent/pyproject.toml", r'^version = "([^"]+)"'),
	("control-plane/nexaploy/__init__.py", r'^__version__ = "([^"]+)"'),
)


def valid_version(version: str) -> str:
	version = version[1:] if version.startswith("v") else version
	if not SEMVER_RE.match(version):
		raise ValueError(f"not a semantic version: {version!r} (e.g. 0.2.0 or 0.2.0-beta.1)")
	return version


def is_prerelease(version: str) -> bool:
	return "-" in valid_version(version)


def current_versions(repo: Path) -> dict[str, str | None]:
	out = {}
	for rel, pattern in VERSION_FILES:
		m = re.search(pattern, (repo / rel).read_text(), re.M)
		out[rel] = m.group(1) if m else None
	return out


def bump(repo: Path, version: str) -> list[str]:
	version = valid_version(version)
	changed = []
	for rel, pattern in VERSION_FILES:
		path = repo / rel
		text = path.read_text()
		new, n = re.subn(pattern, lambda m: m.group(0).replace(m.group(1), version), text, count=1, flags=re.M)
		if n != 1:
			raise ValueError(f"no version string in {rel}")
		if new != text:
			path.write_text(new)
			changed.append(rel)
	return changed


def heading_re(version: str) -> re.Pattern[str]:
	return re.compile(rf"^## \[{re.escape(version)}\](?: - (?P<date>.+?))?[ \t]*$", re.M)


def release_changelog(text: str, version: str, date: str) -> str:
	version = valid_version(version)
	m = heading_re(version).search(text)
	if m:
		if m.group("date") and m.group("date").strip().lower() != "unreleased":
			raise ValueError(f"{version} is already released ({m.group('date').strip()})")
		return text[: m.start()] + f"## [{version}] - {date}" + text[m.end():]
	unreleased = re.search(r"^## \[Unreleased\][ \t]*$", text, re.M)
	if not unreleased:
		raise ValueError(f"CHANGELOG has neither a [{version}] nor an [Unreleased] section")
	body_start = unreleased.end()
	nxt = re.search(r"^## \[", text[body_start:], re.M)
	body = text[body_start: body_start + nxt.start()] if nxt else text[body_start:]
	if not body.strip():
		raise ValueError("the [Unreleased] section is empty")
	return text[: unreleased.start()] + f"## [Unreleased]\n\n## [{version}] - {date}" + text[unreleased.end():]


def release_notes(text: str, version: str) -> str:
	version = valid_version(version)
	m = heading_re(version).search(text)
	if not m:
		raise ValueError(f"no [{version}] section in the CHANGELOG")
	rest = text[m.end():]
	nxt = re.search(r"^(## \[|\[[^\]]+\]: )", rest, re.M)
	return (rest[: nxt.start()] if nxt else rest).strip() + "\n"


def main(argv: list[str]) -> int:
	if len(argv) < 2:
		print(__doc__, file=sys.stderr)
		return 2
	cmd, args = argv[1], argv[2:]
	try:
		if cmd == "bump" and len(args) == 2:
			for rel in bump(Path(args[0]), args[1]):
				print(f"updated {rel}")
		elif cmd == "check" and len(args) == 2:
			version = valid_version(args[1])
			bad = {k: v for k, v in current_versions(Path(args[0])).items() if v != version}
			if bad:
				for rel, found in bad.items():
					print(f"{rel}: {found} != {version}", file=sys.stderr)
				return 1
		elif cmd == "changelog" and len(args) == 3:
			path = Path(args[0])
			path.write_text(release_changelog(path.read_text(), args[1], args[2]))
		elif cmd == "notes" and len(args) == 2:
			sys.stdout.write(release_notes(Path(args[0]).read_text(), args[1]))
		elif cmd == "prerelease" and len(args) == 1:
			print("true" if is_prerelease(args[0]) else "false")
		else:
			print(__doc__, file=sys.stderr)
			return 2
	except (OSError, ValueError) as exc:
		print(f"error: {exc}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv))
