"""External uptime monitoring and public status pages (ARCHITECTURE.md §16).

    ensure_default_monitors()       # */5 via tasks.check_sites_uptime: every Active site gets one
    run_builtin_probe()             # every minute: the control plane's own probe runs due checks
    ingest(probe, results)          # results of any probe -> raw rows, hourly rollups, state machine
    prune_monitoring_data()         # daily: raw 48 h, rollups 90 days, incidents 365 days
    status_page_data(name)          # public, cached 60 s (frappe.cache)

Where checks run from ("probes", DocType Uptime Probe):

* ``control-plane`` (built-in, created on first use) - the control plane's scheduler. For hosted
  servers it is outside the customer's server. On an **all-in-one** install it shares the host
  with the sites of that server: those results are flagged ``same_host`` in the portal, and the
  operator should add at least one external probe.
* external ``nexaploy-probe`` workers (nexaploy/probe.py, stdlib only) on cheap VPSes in other
  regions, authenticated with a per-probe token (nexaploy.api.monitoring_probe).

Alerting: a site's default monitor (is_default) emits site_down / site_recovered exactly like
the old 5-minute ping (and keeps Site.is_up / last_uptime_check up to date); every other
monitor emits monitor_down / monitor_up. One alert per incident: a monitor only alerts when
the quorum decision changes its status (pure state machine in nexaploy.monitor_utils).
"""

import json
from concurrent.futures import ThreadPoolExecutor

import frappe
from frappe.utils import add_to_date, get_url, now_datetime

from nexaploy import monitor_utils as mu
from nexaploy import notify_utils as nu

MONITOR = "Uptime Monitor"
PROBE = "Uptime Probe"
RESULT = "Uptime Check Result"
ROLLUP = "Uptime Rollup"
INCIDENT = "Uptime Incident"
STATUS_PAGE = "Status Page"
BUILTIN_WORKERS = 16
MONITOR_FIELDS = [
	"name", "title", "team", "site", "enabled", "is_default", "status", "url", "method",
	"expected_status", "keyword", "interval_minutes", "timeout_seconds", "failure_threshold",
	"probes", "quorum", "probe_state", "open_incident", "builtin_checked_at",
]


# --------------------------------------------------------------------------- lookups


def site_hosts(site):
	"""The site's primary host and its Active custom domains (the only monitor targets)."""
	hosts = [site] if site else []
	for d in frappe.get_all("Site Domain", filters={"parent": site, "parenttype": "Site", "status": "Active"}, pluck="domain"):
		if d and d not in hosts:
			hosts.append(d)
	return hosts


def advanced_monitoring(team, site=None):
	"""Paid tier: Team.advanced_monitoring, or the site's Nexaploy Plan has the flag."""
	try:
		if team and frappe.db.get_value("Team", team, "advanced_monitoring"):
			return True
		if site:
			plan = frappe.db.get_value("Site", site, "plan")
			if plan and frappe.db.get_value("Nexaploy Plan", plan, "advanced_monitoring"):
				return True
	except Exception:
		pass  # columns not migrated yet
	return False


def builtin_probe():
	"""The control plane's own probe (created on first use, enabled by default)."""
	if not frappe.db.exists(PROBE, mu.BUILTIN_PROBE):
		region = "Control plane"
		try:
			from nexaploy.all_in_one import control_plane_host

			if control_plane_host():
				region = "Control plane (same host as the all-in-one server)"
		except Exception:
			pass
		doc = frappe.get_doc({
			"doctype": PROBE, "probe_name": mu.BUILTIN_PROBE, "title": "Control plane", "region": region,
			"enabled": 1,
		})
		doc.insert(ignore_permissions=True)
	return frappe.db.get_value(PROBE, mu.BUILTIN_PROBE, ["name", "enabled", "is_builtin", "region"], as_dict=True)


def enabled_probe_names(include_disabled=False):
	filters = {} if include_disabled else {"enabled": 1}
	names = frappe.get_all(PROBE, filters=filters, pluck="name", order_by="creation asc")
	if not names and not include_disabled:
		probe = builtin_probe()
		names = [probe.name] if probe and probe.enabled else []
	return names


def probe_rows():
	cp_host = _cp_host_server()
	rows = frappe.get_all(
		PROBE, filters={"enabled": 1}, fields=["name", "title", "region", "is_builtin", "last_seen_at"], order_by="creation asc"
	)
	for r in rows:
		r["cp_host_server"] = cp_host if r.is_builtin else None
	return rows


def _cp_host_server():
	try:
		from nexaploy.all_in_one import control_plane_host

		host = control_plane_host()
		return host.name if host else None
	except Exception:
		return None


def _active_sites():
	return set(frappe.get_all("Site", filters={"status": "Active"}, pluck="name"))


# --------------------------------------------------------------------------- default monitors


def ensure_default_monitors():
	"""Every Active site has exactly one default monitor; monitors of sites that are no longer
	Active are paused (an open incident is closed silently: suspension/deletion is not downtime)
	and resumed when the site is Active again."""
	builtin_probe()
	active = _active_sites()
	existing = {
		r.site: r
		for r in frappe.get_all(MONITOR, filters={"is_default": 1}, fields=["name", "site", "hostname", "status", "enabled", "open_incident"])
	}
	for site in sorted(active - set(existing)):
		team = frappe.db.get_value("Site", site, "team")
		if not team:
			continue
		try:
			doc = frappe.get_doc({"doctype": MONITOR, "team": team, "site": site, **mu.default_monitor(site)})
			doc.flags.ignore_permissions = True
			doc.flags.default_monitor = True
			doc.insert()
		except Exception:
			frappe.log_error(title=f"Nexaploy: default monitor for {site} failed")
	# renamed sites keep their default monitor (Link renamed by Frappe): follow the new host
	for site, row in existing.items():
		if site in active and row.hostname != site:
			d = mu.default_monitor(site)
			frappe.db.set_value(MONITOR, row.name, {"hostname": d["hostname"], "url": d["url"]}, update_modified=False)
	# transferred sites: their monitors follow the site's team (past incidents stay with the old team)
	frappe.db.sql(
		"""update `tabUptime Monitor` m join `tabSite` s on s.name = m.site
		set m.team = s.team where s.team is not null and m.team != s.team"""
	)
	now = now_datetime()
	for name in frappe.get_all(MONITOR, filters={"status": ["!=", mu.PAUSED]}, fields=["name", "site", "open_incident"]):
		if name.site in active:
			continue
		if name.open_incident:
			resolve_incident(name.open_incident, now, "Site is no longer active")
		frappe.db.set_value(
			MONITOR, name.name,
			{"status": mu.PAUSED, "open_incident": None, "probe_state": None, "status_changed_at": now},
			update_modified=False,
		)
	for row in frappe.get_all(MONITOR, filters={"status": mu.PAUSED, "enabled": 1}, fields=["name", "site"]):
		if row.site in active:
			frappe.db.set_value(MONITOR, row.name, {"status": mu.PENDING, "status_changed_at": now}, update_modified=False)
	frappe.db.commit()


# --------------------------------------------------------------------------- probes


def monitors_for_probe(probe_name):
	"""Enabled monitors of Active sites this probe checks (as check dicts for probe.py)."""
	enabled = enabled_probe_names()
	if probe_name not in enabled:
		return []
	active = _active_sites()
	out = []
	for m in frappe.get_all(MONITOR, filters={"enabled": 1}, fields=MONITOR_FIELDS):
		if m.site not in active or probe_name not in mu.assigned_probes(m.probes, enabled):
			continue
		out.append(m)
	return out


def check_payload(m):
	return {
		"monitor": m.name,
		"url": m.url,
		"method": m.method or "GET",
		"expected_status": m.expected_status or mu.DEFAULT_EXPECTED,
		"keyword": m.keyword or "",
		"timeout": int(m.timeout_seconds or mu.DEFAULT_TIMEOUT),
		"interval": int(m.interval_minutes or mu.DEFAULT_INTERVAL),
	}


def run_builtin_probe():
	"""Every minute: run the checks that are due on the built-in probe, then ingest them."""
	from nexaploy.probe import run_checks

	probe = builtin_probe()
	if not probe or not probe.enabled:
		return
	now = now_datetime()
	due = [m for m in monitors_for_probe(probe.name) if mu.is_due(m.builtin_checked_at, m.interval_minutes, now)]
	if not due:
		return
	for m in due:
		frappe.db.set_value(MONITOR, m.name, "builtin_checked_at", now, update_modified=False)
	frappe.db.commit()
	results = run_checks([check_payload(m) for m in due], workers=BUILTIN_WORKERS)
	frappe.db.set_value(PROBE, probe.name, "last_seen_at", now_datetime(), update_modified=False)
	ingest(probe.name, results)


def ingest(probe_name, raw_results):
	"""Store the results of one probe and advance each monitor's state machine.
	Returns the number of accepted results. Results for monitors the probe does not check are
	dropped (a probe token can only report on what it was asked to check)."""
	now = now_datetime()
	results = [r for r in (mu.clean_result(x, now) for x in raw_results or [] if isinstance(x, dict)) if r]
	if not results:
		return 0
	allowed = {m.name: m for m in monitors_for_probe(probe_name)}
	results = [r for r in results if r["monitor"] in allowed]
	if not results:
		return 0
	_store_raw(probe_name, results, allowed)
	_store_rollups(results, allowed)
	by_monitor = {}
	for r in sorted(results, key=lambda r: r["checked_at"]):
		by_monitor.setdefault(r["monitor"], []).append(r)
	enabled = enabled_probe_names()
	for name, rows in by_monitor.items():
		try:
			_advance(name, probe_name, rows, enabled, now)
		except Exception:
			frappe.log_error(title=f"Nexaploy: uptime state of {name} failed")
	frappe.db.commit()
	return len(results)


def _store_raw(probe_name, results, monitors):
	fields = ["name", "creation", "modified", "owner", "modified_by", "monitor", "team", "probe", "checked_at",
	          "ok", "status_code", "response_ms", "error"]
	now = now_datetime()
	values = [
		(frappe.generate_hash(length=12), now, now, "Administrator", "Administrator", r["monitor"],
		 monitors[r["monitor"]].team, probe_name, r["checked_at"], r["ok"], r["status_code"], r["response_ms"], r["error"])
		for r in results
	]
	frappe.db.bulk_insert(RESULT, fields, values, ignore_duplicates=True)


def _store_rollups(results, monitors):
	now = now_datetime()
	for (monitor, hour), agg in mu.rollup(results).items():
		frappe.db.sql(
			"""insert into `tabUptime Rollup`
				(name, rollup_key, creation, modified, owner, modified_by, docstatus, monitor, team, hour,
				 checks, failures, response_ms_sum, response_count, max_response_ms)
			values (%(key)s, %(key)s, %(now)s, %(now)s, 'Administrator', 'Administrator', 0, %(monitor)s, %(team)s,
				%(hour)s, %(checks)s, %(failures)s, %(sum)s, %(count)s, %(max)s)
			on duplicate key update
				checks = checks + values(checks), failures = failures + values(failures),
				response_ms_sum = response_ms_sum + values(response_ms_sum),
				response_count = response_count + values(response_count),
				max_response_ms = greatest(max_response_ms, values(max_response_ms)), modified = values(modified)""",
			{
				"key": mu.rollup_key(monitor, hour), "now": now, "monitor": monitor, "team": monitors[monitor].team,
				"hour": hour, "checks": agg["checks"], "failures": agg["failures"], "sum": agg["response_ms_sum"],
				"count": agg["response_count"], "max": agg["max_response_ms"],
			},
		)


def _load_state(value):
	if isinstance(value, dict):
		return value
	try:
		data = json.loads(value or "{}")
	except (TypeError, ValueError):
		data = {}
	return data if isinstance(data, dict) else {}


def _advance(name, probe_name, rows, enabled_probes, now):
	# row lock: probes report concurrently; the state JSON must not lose updates
	m = frappe.db.get_value(MONITOR, name, MONITOR_FIELDS, as_dict=True, for_update=True)
	if not m or not m.enabled or m.status == mu.PAUSED:
		return
	state = _load_state(m.probe_state)
	for r in rows:
		state = mu.apply_result(state, probe_name, r)
	assigned = mu.assigned_probes(m.probes, enabled_probes)
	decision = mu.decide(state, now, m.interval_minutes, m.failure_threshold, m.quorum, assigned)
	new_status, action = mu.next_status(m.status, decision)
	last = rows[-1]
	values = {
		"probe_state": json.dumps(state, sort_keys=True),
		"last_checked_at": last["checked_at"],
		# no status code / timing when DNS or the connection failed (Int columns are NOT NULL)
		"last_status_code": last["status_code"] or 0,
		"last_response_ms": last["response_ms"] or 0,
		"last_error": last["error"] or None,
	}
	if last.get("tls_expires_on"):
		values["tls_expires_on"] = last["tls_expires_on"]
	if new_status != m.status:
		values["status"] = new_status
		values["status_changed_at"] = now
	if action == "open":
		cause = mu.failing_summary(state, now, m.interval_minutes) or last["error"]
		started = mu.failure_started(state, now, m.interval_minutes, now)
		values["open_incident"] = _open_incident(m, started, cause)
	elif action == "close" and m.open_incident:
		incident = resolve_incident(m.open_incident, now)
		values["open_incident"] = None
		m["_duration"] = incident.get("duration_seconds") if incident else None
	frappe.db.set_value(MONITOR, name, values, update_modified=False)
	if m.is_default and new_status in (mu.UP, mu.DOWN):
		frappe.db.set_value(
			"Site", m.site, {"is_up": int(new_status == mu.UP), "last_uptime_check": now}, update_modified=False
		)
	if new_status != m.status:
		clear_team_status_cache(m.team)
	if action:
		_notify(m, action, values.get("open_incident") or m.open_incident, now)


def _open_incident(m, now, cause):
	doc = frappe.get_doc({
		"doctype": INCIDENT, "monitor": m.name, "team": m.team, "site": m.site, "status": mu.OPEN,
		"started_at": now, "cause": mu.truncate(cause, 500),
	})
	doc.insert(ignore_permissions=True)
	return doc.name


def resolve_incident(name, now, note=None):
	inc = frappe.db.get_value(INCIDENT, name, ["name", "started_at", "status", "cause"], as_dict=True)
	if not inc or inc.status == mu.RESOLVED:
		return inc
	duration = int((now - inc.started_at).total_seconds()) if inc.started_at else 0
	values = {"status": mu.RESOLVED, "resolved_at": now, "duration_seconds": duration}
	if note:
		values["cause"] = mu.truncate(f"{inc.cause or ''} ({note})".strip(), 500)
	frappe.db.set_value(INCIDENT, name, values)
	inc.update(values)
	return inc


def _notify(m, action, incident, now):
	from nexaploy.notify import notify

	down = action == "open"
	if m.is_default:
		event = nu.SITE_DOWN if down else nu.SITE_RECOVERED
		label = m.site
	else:
		event = nu.MONITOR_DOWN if down else nu.MONITOR_UP
		label = m.title
	if down:
		title = f"{label} is DOWN"
		message = f"{m.url} stopped responding at {now} UTC."
	else:
		title = f"{label} is back UP"
		duration = m.get("_duration")
		message = f"{m.url} responds again at {now} UTC" + (f" after {mu.format_duration(duration)}." if duration else ".")
	notify(
		m.team, event, title, message,
		{"site": m.site, "monitor": m.name, "status": "down" if down else "up", "incident": incident},
		get_url(f"/dashboard/monitoring?monitor={m.name}"),
	)


# --------------------------------------------------------------------------- housekeeping


def prune_monitoring_data():
	"""Daily: raw results after 48 h, hourly rollups after 90 days, resolved incidents after a year."""
	now = now_datetime()
	frappe.db.delete(RESULT, {"checked_at": ["<", add_to_date(now, hours=-mu.RAW_RETENTION_HOURS)]})
	frappe.db.delete(ROLLUP, {"hour": ["<", add_to_date(now, days=-mu.ROLLUP_RETENTION_DAYS)]})
	old = frappe.get_all(
		INCIDENT,
		filters={"status": mu.RESOLVED, "resolved_at": ["<", add_to_date(now, days=-mu.INCIDENT_RETENTION_DAYS)]},
		pluck="name",
	)
	for name in old:
		frappe.delete_doc(INCIDENT, name, ignore_permissions=True, force=True)
	frappe.db.commit()


# --------------------------------------------------------------------------- uptime / incidents


def incidents_since(monitors, since):
	"""Incidents overlapping [since, now) for the given monitor names."""
	if not monitors:
		return []
	return frappe.get_all(
		INCIDENT,
		filters={"monitor": ["in", list(monitors)]},
		or_filters={"status": mu.OPEN, "resolved_at": [">=", since]},
		fields=["name", "monitor", "status", "started_at", "resolved_at", "duration_seconds", "public_title",
		        "hide_from_status_page", "cause"],
		order_by="started_at desc",
		limit_page_length=0,
	)


def uptime_by_monitor(monitors, days):
	"""{monitor name: daily_uptime rows} for monitor rows {name, creation}."""
	now = now_datetime()
	since = add_to_date(now, days=-days)
	incidents = incidents_since([m.name for m in monitors], since)
	out = {}
	for m in monitors:
		mine = [i for i in incidents if i.monitor == m.name]
		out[m.name] = mu.daily_uptime(mine, now, days, m.creation)
	return out


# --------------------------------------------------------------------------- status pages


def clear_status_cache(page_name):
	try:
		frappe.cache().delete_keys(f"nexaploy:status_page:{page_name}:")
	except Exception:
		pass


def clear_team_status_cache(team):
	try:
		for name in frappe.get_all(STATUS_PAGE, filters={"team": team}, pluck="name"):
			clear_status_cache(name)
	except Exception:
		pass


def resolve_status_page(slug=None, host=None):
	"""Name of the published status page for /status/<slug>, or for a request host that is a
	page's status_domain."""
	if slug:
		try:
			slug = mu.validate_slug(slug)
		except mu.MonitorError:
			return None
		return frappe.db.get_value(STATUS_PAGE, {"slug": slug, "enabled": 1}, "name")
	host = mu.normalize_host(host)
	if host:
		return frappe.db.get_value(STATUS_PAGE, {"status_domain": host, "enabled": 1}, "name")
	return None


def status_brand(team):
	"""A brand the team owns (enabled) > the team's reseller brand > the platform default."""
	from nexaploy import brand_utils as bu
	from nexaploy.branding import ROW_FIELDS, team_brand

	try:
		row = frappe.db.get_value("Brand", {"team": team, "enabled": 1}, ROW_FIELDS, as_dict=True)
	except Exception:
		row = None
	return bu.public_brand(row) if row else team_brand(team)


def status_page_data(page_name, lang="en"):
	"""Everything the public page renders (cached 60 s). Contains no internal data: no URLs,
	hosts, probe details or error texts - only public names, statuses and written updates."""
	key = mu.status_cache_key(page_name, lang)
	cache = frappe.cache()
	try:
		data = cache.get_value(key)
	except Exception:
		data = None
	if isinstance(data, dict):
		return data
	data = _build_status_page(page_name)
	try:
		cache.set_value(key, data, expires_in_sec=mu.STATUS_CACHE_SECONDS)
	except Exception:
		pass
	return data


def _build_status_page(page_name):
	page = frappe.get_doc(STATUS_PAGE, page_name)
	rows = [r for r in page.monitors if r.monitor]
	monitors = {
		m.name: m
		for m in frappe.get_all(
			MONITOR,
			filters={"name": ["in", [r.monitor for r in rows] or [""]], "team": page.team},
			fields=["name", "title", "status", "enabled", "creation"],
		)
	}
	shown = [(r, monitors[r.monitor]) for r in rows if r.monitor in monitors]
	days = uptime_by_monitor([m for _, m in shown], mu.STATUS_DAYS)
	public_monitors = [mu.public_monitor(m, r.display_name or m.title, days[m.name]) for r, m in shown]
	names = {m.name: (r.display_name or m.title) for r, m in shown}
	incidents = []
	if page.show_incident_history and shown:
		since = add_to_date(now_datetime(), days=-mu.STATUS_DAYS)
		for inc in incidents_since(list(names), since)[:50]:
			if inc.hide_from_status_page:
				continue
			updates = frappe.get_all(
				"Status Incident Update",
				filters={"parent": inc.name, "parenttype": INCIDENT},
				fields=["status", "message", "posted_at"],
				order_by="posted_at desc",
			)
			incidents.append(mu.public_incident(inc, names[inc.monitor], updates))
	brand = status_brand(page.team)
	return {
		"title": page.title,
		"description": page.description or "",
		"brand": {k: brand.get(k) for k in ("title", "logo", "favicon", "primary_color", "css", "support_email", "footer_text")},
		"overall": mu.overall_status([m["status"] for m in public_monitors]),
		"monitors": public_monitors,
		"incidents": incidents,
		"active_incidents": [i for i in incidents if i["status"] == mu.OPEN],
		"past_incidents": [i for i in incidents if i["status"] != mu.OPEN],
		"updated_at": mu.iso(now_datetime()),
		"days": mu.STATUS_DAYS,
	}
