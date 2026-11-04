"""scripts/release_utils.py and scripts/release.sh (run: python -m pytest scripts/tests -q)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import release_utils as ru  # noqa: E402


@pytest.fixture
def repo(tmp_path):
	for rel, _ in ru.VERSION_FILES:
		(tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
		shutil.copy(REPO / rel, tmp_path / rel)
	# a fixed, unreleased CHANGELOG: the real one gets dated when a version ships
	(tmp_path / "CHANGELOG.md").write_text(
		"# Changelog\n\n## [0.1.0] - Unreleased\n\n### Added\n\n- Agent upgrades from the control plane.\n"
	)
	(tmp_path / "scripts").mkdir()
	shutil.copy(SCRIPTS / "release.sh", tmp_path / "scripts" / "release.sh")
	shutil.copy(SCRIPTS / "release_utils.py", tmp_path / "scripts" / "release_utils.py")
	return tmp_path


def test_repo_versions_are_in_sync():
	versions = set(ru.current_versions(REPO).values())
	assert len(versions) == 1 and None not in versions, versions


def test_changelog_has_current_version():
	version = next(iter(ru.current_versions(REPO).values()))
	assert ru.release_notes((REPO / "CHANGELOG.md").read_text(), version).strip()


def test_valid_version():
	assert ru.valid_version("v1.2.3") == "1.2.3"
	assert ru.is_prerelease("0.2.0-beta.1") and not ru.is_prerelease("0.2.0")
	for bad in ("1.2", "01.2.3", "latest", "1.2.3.4"):
		with pytest.raises(ValueError):
			ru.valid_version(bad)


def test_bump_and_check(repo):
	assert set(ru.bump(repo, "0.2.0")) == {rel for rel, _ in ru.VERSION_FILES}
	assert set(ru.current_versions(repo).values()) == {"0.2.0"}
	assert ru.bump(repo, "0.2.0") == []
	assert ru.main(["x", "check", str(repo), "0.2.0"]) == 0
	assert ru.main(["x", "check", str(repo), "0.3.0"]) == 1
	# only the version line changes
	assert 'name = "nexaploy-agent"' in (repo / "agent/pyproject.toml").read_text()


CL = """# Changelog

## [Unreleased]

### Added
- thing

## [0.1.0] - 2026-11-01

### Added
- first

[0.1.0]: https://example.com/v0.1.0
"""


def test_changelog_release_and_notes():
	out = ru.release_changelog(CL, "0.2.0", "2026-12-01")
	assert "## [Unreleased]\n\n## [0.2.0] - 2026-12-01\n\n### Added\n- thing" in out
	assert ru.release_notes(out, "0.2.0") == "### Added\n- thing\n"
	assert ru.release_notes(out, "0.1.0") == "### Added\n- first\n"
	with pytest.raises(ValueError):
		ru.release_changelog(CL, "0.1.0", "2026-12-01")  # already released
	with pytest.raises(ValueError):
		ru.release_changelog("## [Unreleased]\n\n## [0.1.0] - x\n", "0.2.0", "d")  # empty
	dated = ru.release_changelog("## [0.3.0] - Unreleased\n\n- x\n", "0.3.0", "2027-01-02")
	assert dated.startswith("## [0.3.0] - 2027-01-02")
	with pytest.raises(ValueError):
		ru.release_notes(CL, "9.9.9")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_release_sh_commits_and_tags(repo):
	env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
	       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": "/dev/null"}
	run = lambda *a: subprocess.run(a, cwd=repo, env=env, capture_output=True, text=True)  # noqa: E731
	run("git", "init", "-q")
	run("git", "add", "-A")
	assert run("git", "commit", "-qm", "init").returncode == 0
	res = run("bash", "scripts/release.sh", "0.1.0", "--date", "2026-12-24")
	assert res.returncode == 0, res.stderr
	assert "## [0.1.0] - 2026-12-24" in (repo / "CHANGELOG.md").read_text()
	assert run("git", "log", "-1", "--format=%s").stdout.strip() == "Release v0.1.0"
	tag = run("git", "tag", "-n99", "v0.1.0").stdout
	assert "Agent upgrades from the control plane" in tag
	res = run("bash", "scripts/release.sh", "0.1.0")
	assert res.returncode != 0 and "already exists" in res.stderr
	res = run("bash", "scripts/release.sh", "nope", "--no-commit")
	assert res.returncode != 0
