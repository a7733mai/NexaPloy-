"""Pure logic of the "Safe update" flow (Site Update Test).

No frappe import: unit-tested with plain pytest (tests/test_update_test.py).
nexaploy/update_test.py drives the chain; nexaploy/api/update_test.py is the portal API.

Flow of one Site Update Test (status):

    Pending -> [Preparing Bench] -> Cloning -> Testing -> Passed | Failed
    Passed -> Applying -> Applied | Apply Failed
    any state except Applying/Applied -> Cancelled

- Preparing Bench: a bench of the site's release group running the new image is created on the
  site's server (bench.create) when none exists yet;
- Cloning: site.clone {bench: <production bench>, site, target_site: uptest-<site>,
  target_bench: <new bench>, scrub} - backup, restore on the new image, scrub, **migrate**;
- Testing: site.smoke_check on the copy (HTTP 200 on /api/method/ping and /login, a database
  query, every app installed);
- Applying: Site.move_to_bench(target_bench, allow_upgrade) on production - the move takes a
  fresh backup first (Site Backup purpose Move). Production is never touched before that.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from nexaploy.utils import is_valid_site_name, unique_bench_name

STATUSES = (
	"Pending", "Preparing Bench", "Cloning", "Testing", "Passed", "Failed",
	"Applying", "Applied", "Apply Failed", "Cancelled",
)
RUNNING = ("Pending", "Preparing Bench", "Cloning", "Testing")
IN_PROGRESS = RUNNING + ("Applying",)
FINAL = ("Failed", "Applied", "Apply Failed", "Cancelled")
CANCELLABLE = RUNNING + ("Passed", "Failed")
TEST_PREFIX = "uptest-"
SMOKE_PATHS = ("/api/method/ping", "/login")
SMOKE_TIMEOUT = 20
MAX_LOG_CHARS = 200_000
MAX_SECTION_CHARS = 60_000


# --------------------------------------------------------------------------- naming


def copy_site_name(site_name: str) -> str:
	"""`erp.example.com` -> `uptest-erp.example.com` (prefix on the first label)."""
	name = str(site_name or "").strip().lower()
	if not is_valid_site_name(name):
		raise ValueError("Invalid site name")
	first, _, rest = name.partition(".")
	label = f"{TEST_PREFIX}{first}"
	out = f"{label}.{rest}"
	if len(label) > 63 or not is_valid_site_name(out):
		raise ValueError("Site name is too long for an update test copy")
	return out


def new_bench_name(release_group: str, existing: Iterable[str], suffix: str) -> str:
	"""Name of a bench created for the new image: `<group>-<suffix>` (bench regex, unique)."""
	return unique_bench_name(release_group, existing, suffix)


# --------------------------------------------------------------------------- target bench


def pick_target_bench(benches: Iterable[Mapping[str, Any]], server: str, release_group: str,
                      image_tag: str, exclude: str | None = None) -> str | None:
	"""An Active bench on `server` of `release_group` already running `image_tag` (newest
	first in `benches`), not the site's own bench."""
	for b in benches:
		if (
			b.get("status") == "Active"
			and b.get("server") == server
			and b.get("release_group") == release_group
			and b.get("image_tag") == image_tag
			and b.get("name") != exclude
		):
			return b.get("name")
	return None


def update_available(current_image: str | None, latest_image: str | None) -> bool:
	return bool(latest_image) and latest_image != current_image


def validate_target(site_server: str, source_bench: str, target: Mapping[str, Any] | None) -> None:
	"""A test runs on another Active bench of the same server (the copy is cloned from the
	production bench's local backup). Raises ValueError."""
	if not target:
		raise ValueError("Target bench not found")
	if target.get("name") == source_bench:
		raise ValueError("The site already runs on this bench")
	if target.get("status") != "Active":
		raise ValueError("Target bench is not active")
	if target.get("server") != site_server:
		raise ValueError(
			"The test copy is created on the site's own server: choose a bench on the same server "
			"(move the site to another server first, then test the update there)"
		)


# --------------------------------------------------------------------------- jobs


def clone_job_params(source_bench: str, site: str, test_site: str, target_bench: str) -> dict[str, Any]:
	params: dict[str, Any] = {"bench": source_bench, "site": site, "target_site": test_site, "scrub": True}
	if target_bench and target_bench != source_bench:
		params["target_bench"] = target_bench
	return params


def smoke_params(test_site: str, apps: Iterable[str] = ()) -> dict[str, Any]:
	return {
		"site": test_site,
		"paths": list(SMOKE_PATHS),
		"timeout": SMOKE_TIMEOUT,
		"apps": sorted({a for a in apps if a}),
	}


def failed_step(steps: Iterable[Mapping[str, Any]]) -> str | None:
	"""Name of the first failed step of an agent job (steps: step_name/name + status)."""
	for s in sorted(steps or [], key=lambda s: s.get("step_idx", s.get("idx", 0)) or 0):
		if str(s.get("status") or "").lower() == "failure":
			return s.get("step_name") or s.get("name")
	return None


def step_output(steps: Iterable[Mapping[str, Any]], name: str) -> str:
	for s in steps or []:
		if (s.get("step_name") or s.get("name")) == name:
			return str(s.get("output") or "")
	return ""


def clone_failure(steps: Iterable[Mapping[str, Any]], error: str | None) -> dict[str, str]:
	"""{stage, message, detail} for a failed site.clone of the test copy."""
	steps = list(steps or [])
	step = failed_step(steps)
	error = (error or "").strip()
	if step == "migrate":
		output = step_output(steps, "migrate").strip()
		return {
			"stage": "migrate",
			"message": "Migration failed on the new version",
			"detail": tail(output or error, 8000),
		}
	if step in ("backup", "stage"):
		message = "Backing up the production site failed"
	elif step in ("new_site", "restore", "restore_config"):
		message = "Restoring the copy on the new bench failed"
	elif step == "scrub":
		message = "Preparing the copy (muting emails) failed"
	else:
		message = "Creating the test copy failed"
	return {"stage": step or "clone", "message": message, "detail": tail(error, 4000)}


def smoke_outcome(result: Any, error: str | None = None) -> dict[str, Any]:
	"""{ok, checks, summary} from a site.smoke_check result."""
	result = result if isinstance(result, Mapping) else {}
	checks = [c for c in (result.get("checks") or []) if isinstance(c, Mapping)]
	ok = bool(result.get("ok")) and bool(checks) and all(c.get("ok") for c in checks)
	summary = result.get("summary") or (error or "")
	if not checks and error:
		checks = [{"name": "smoke check", "ok": False, "detail": error}]
	return {"ok": ok, "checks": [dict(c) for c in checks], "summary": str(summary or "")}


def tail(text: str | None, limit: int) -> str:
	text = text or ""
	return text if len(text) <= limit else "…" + text[-limit:]


def format_log(sections: Iterable[tuple[str, str | None]]) -> str:
	"""`== title ==` sections, each and the whole capped (tail kept: errors are at the end)."""
	parts = []
	for title, text in sections:
		parts.append(f"== {title} ==")
		parts.append(tail((text or "").rstrip(), MAX_SECTION_CHARS) or "(no output)")
	return tail("\n".join(parts), MAX_LOG_CHARS)


def append_log(current: str | None, title: str, text: str | None) -> str:
	block = format_log([(title, text)])
	return tail(f"{current.rstrip()}\n{block}" if current else block, MAX_LOG_CHARS)


# --------------------------------------------------------------------------- apply


def apply_block_reason(status: str, site: Mapping[str, Any], source_bench: str,
                       target_status: str | None) -> str | None:
	"""Why "Apply update" is not possible now (None: go)."""
	if status != "Passed":
		return "Only a passed test can be applied"
	if site.get("status") != "Active":
		return "The site must be Active"
	if site.get("bench") != source_bench:
		return "The site moved since the test ran; run a new test"
	if target_status != "Active":
		return "The tested bench is no longer active; run a new test"
	return None


def apply_outcome(site: Mapping[str, Any], source_bench: str, target_bench: str) -> str | None:
	"""Status of an Applying test from the production Site: Applied / Apply Failed / None."""
	status = site.get("status")
	if status == "Moving":
		return None
	if status == "Active" and site.get("bench") == target_bench:
		return "Applied"
	if status == "Broken":
		return "Apply Failed"
	if status == "Active" and site.get("bench") == source_bench and not site.get("move_steps"):
		return "Apply Failed"  # move aborted before the source was dropped: production untouched
	return None


# --------------------------------------------------------------------------- automatic updates


def auto_gate(site_names: Iterable[str], tests: Mapping[str, str | None]) -> dict[str, Any]:
	"""Decide an auto update of a bench whose group requires a passing test per site.

	`tests` maps site -> status of its latest test for the new build (None: no test yet).
	Returns {action: apply | wait | blocked, start: [sites needing a test], failed: [...]}.
	"""
	start, failed, waiting = [], [], []
	for site in site_names:
		status = tests.get(site)
		if status is None or status == "Cancelled":
			start.append(site)
		elif status in ("Failed", "Apply Failed"):
			failed.append(site)
		elif status in ("Passed", "Applied"):
			continue
		else:
			waiting.append(site)
	if failed:
		action = "blocked"
	elif start or waiting:
		action = "wait"
	else:
		action = "apply"
	return {"action": action, "start": start, "failed": failed, "waiting": waiting}
