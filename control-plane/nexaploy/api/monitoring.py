"""Uptime monitoring and the public status page of the current team (/dashboard/monitoring).

Team actions (ARCHITECTURE.md §9): `read` for everything shown, `write` to change monitors,
incident updates and the status page. Monitors can only target the primary host or an Active
custom domain of one of the team's sites. Every mutation is written to the activity log.

    overview()                                  -> monitors (+30-day uptime), sites, probes, limits, status page
    get_monitor(name, hours=24)                 -> detail, hourly response-time series, probe states, incidents, recent checks
    save_monitor(monitor)                       # dict; `name` updates, else creates
    delete_monitor(name)                        # a site's default monitor can only be disabled
    list_incidents(limit=50)
    update_incident(name, public_title=None, hide_from_status_page=None)
    add_incident_update(name, update)           # {status, message} shown on the status page
    get_status_page()                           -> page (or a suggestion) + URL
    save_status_page(page)                      # {title, slug, description, status_domain, enabled,
                                                #  show_incident_history, monitors: [{monitor, display_name}]}
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, get_url, now_datetime

from nexaploy import monitor_utils as mu
from nexaploy import monitoring as mon
from nexaploy.api.dashboard import _ctx
from nexaploy.team_utils import role_allows
from nexaploy.teams import check_team_access

MONITOR = mon.MONITOR
INCIDENT = mon.INCIDENT
STATUS_PAGE = mon.STATUS_PAGE
LIST_FIELDS = [
	"name", "title", "site", "hostname", "path", "url", "method", "expected_status", "keyword", "interval_minutes",
	"timeout_seconds", "failure_threshold", "probes", "quorum", "enabled", "is_default", "status", "last_checked_at",
	"last_status_code", "last_response_ms", "last_error", "tls_expires_on", "open_incident", "status_changed_at",
	"creation",
]


def _throw(exc):
	frappe.throw(_(str(exc)))


def _log(team, action, name, details=None, doctype=MONITOR):
	try:
		from nexaploy.activity import log_activity

		log_activity(team, action, doctype, name, details)
	except Exception:
		pass


def _parse(value, what="data"):
	if isinstance(value, str):
		value = frappe.parse_json(value) if value.strip() else {}
	if not isinstance(value, dict):
		frappe.throw(_("Invalid {0}").format(what))
	return value


def _monitor(name, action="read"):
	c = _ctx(action)
	if not name or not frappe.db.exists(MONITOR, name):
		raise frappe.DoesNotExistError(_("Monitor {0} not found").format(name))
	doc = frappe.get_doc(MONITOR, name)
	if doc.team != c.team:
		check_team_access(doc.team, action)
	return doc


def _incident(name, action="read"):
	c = _ctx(action)
	if not name or not frappe.db.exists(INCIDENT, name):
		raise frappe.DoesNotExistError(_("Incident {0} not found").format(name))
	doc = frappe.get_doc(INCIDENT, name)
	if doc.team != c.team:
		check_team_access(doc.team, action)
	return doc


def _team_sites(team):
	return frappe.get_all(
		"Site", filters={"team": team, "status": ["!=", "Archived"]}, fields=["name", "status", "plan", "server"],
		order_by="name asc",
	)


def _probe_view(probes, site_server=None):
	return [
		{"name": p.name, "title": p.title or p.name, "region": p.region or "", "builtin": bool(p.is_builtin),
		 "location": mu.probe_location(p, site_server, p.cp_host_server)}
		for p in probes
	]


def _monitor_view(m, days=None):
	out = {f: m.get(f) for f in LIST_FIELDS}
	out["enabled"] = bool(m.enabled)
	out["is_default"] = bool(m.is_default)
	out["tls_days_left"] = mu.tls_days_left(m.tls_expires_on, now_datetime())
	if days is not None:
		out["uptime_30d"] = mu.uptime_percent(days)
		out["uptime_24h"] = mu.uptime_percent(days[-1:]) if days else None
	return out


@frappe.whitelist()
def overview():
	c = _ctx("read")
	monitors = frappe.get_all(MONITOR, filters={"team": c.team}, fields=LIST_FIELDS, order_by="is_default desc, site asc, creation asc")
	uptime = mon.uptime_by_monitor(monitors, 30)
	team_sites = _team_sites(c.team)
	sites = []
	for s in team_sites:
		sites.append({
			"name": s.name, "status": s.status, "hosts": mon.site_hosts(s.name),
			"advanced": mon.advanced_monitoring(c.team, s.name),
			"monitors": sum(1 for m in monitors if m.site == s.name),
		})
	probes = mon.probe_rows()
	# all-in-one install without external probes: the only probe runs on the sites' own host
	same_host = bool(probes) and all(p.is_builtin for p in probes) and any(
		mu.probe_location(p, s.server, p.cp_host_server) == "same_host" for p in probes for s in team_sites
	)
	return {
		"monitors": [_monitor_view(m, uptime.get(m.name)) for m in monitors],
		"sites": sites,
		"probes": _probe_view(probes),
		"same_host_only": same_host,
		"limits": {"basic": mu.limits(False), "advanced": mu.limits(True)},
		"team_advanced": mon.advanced_monitoring(c.team),
		"intervals": list(mu.INTERVALS),
		"methods": list(mu.METHODS),
		"update_statuses": list(mu.UPDATE_STATUSES),
		"can_write": bool(c.admin or role_allows(c.role, "write")),
		"status_page": _status_page_view(c.team),
	}


@frappe.whitelist()
def get_monitor(name, hours=24):
	doc = _monitor(name, "read")
	hours = max(1, min(int(hours or 24), 24 * 30))
	now = now_datetime()
	start = mu.hour_start(add_to_date(now, hours=-(hours - 1)))
	rows = frappe.get_all(
		mon.ROLLUP, filters={"monitor": doc.name, "hour": [">=", start]},
		fields=["hour", "checks", "failures", "response_ms_sum", "response_count", "max_response_ms"],
	)
	recent = frappe.get_all(
		mon.RESULT, filters={"monitor": doc.name}, fields=["probe", "checked_at", "ok", "status_code", "response_ms", "error"],
		order_by="checked_at desc", limit_page_length=30,
	)
	state = mon._load_state(doc.probe_state)
	server = frappe.db.get_value("Site", doc.site, "server")
	probes = _probe_view(mon.probe_rows(), server)
	for p in probes:
		p["state"] = state.get(p["name"])
		p["assigned"] = p["name"] in mu.assigned_probes(doc.probes, [x["name"] for x in probes])
	days = mon.uptime_by_monitor([doc], 90)[doc.name]
	incidents = frappe.get_all(
		INCIDENT, filters={"monitor": doc.name}, fields=["name", "status", "started_at", "resolved_at", "duration_seconds", "cause"],
		order_by="started_at desc", limit_page_length=20,
	)
	return {
		"monitor": _monitor_view(doc, days[-30:]),
		"series": mu.response_series(rows, start, hours),
		"days": [{"date": d["date"], "uptime": d["uptime"], "level": mu.bar_level(d["uptime"])} for d in days],
		"uptime_90d": mu.uptime_percent(days),
		"probes": probes,
		"recent": recent,
		"incidents": incidents,
	}


@frappe.whitelist(methods=["POST"])
def save_monitor(monitor):
	c = _ctx("write")
	data = _parse(monitor, _("monitor"))
	name = data.get("name")
	if name:
		doc = _monitor(name, "write")
		if doc.is_default:
			# the default monitor keeps its target; only schedule/alerting settings change
			for key in ("hostname", "path", "method", "expected_status", "keyword"):
				data[key] = doc.get(key)
	else:
		site = data.get("site")
		team = frappe.db.get_value("Site", site, "team") if site else None
		if not team:
			frappe.throw(_("Choose a site"))
		check_team_access(team, "write")
		doc = frappe.new_doc(MONITOR)
		doc.site = site
		doc.team = team
	try:
		clean = mu.validate_monitor(data, mon.site_hosts(doc.site), mon.enabled_probe_names(include_disabled=True))
	except mu.MonitorError as exc:
		_throw(exc)
	doc.update(clean)
	doc.flags.ignore_permissions = True
	doc.save()  # the controller re-validates, applies plan limits and closes the incident of a paused monitor
	_log(doc.team, "monitor.update" if name else "monitor.create", doc.name, {"url": doc.url, "interval": doc.interval_minutes})
	mon.clear_team_status_cache(doc.team)
	return _monitor_view(doc)


@frappe.whitelist(methods=["POST"])
def delete_monitor(name):
	doc = _monitor(name, "write")
	if doc.is_default:
		frappe.throw(_("The default monitor of a site cannot be deleted; disable it instead"))
	team = doc.team
	frappe.delete_doc(MONITOR, doc.name, ignore_permissions=True)
	_log(team, "monitor.delete", name, {"url": doc.url})
	mon.clear_team_status_cache(team)
	return {"ok": True}


@frappe.whitelist()
def list_incidents(limit=50):
	c = _ctx("read")
	limit = max(1, min(int(limit or 50), 200))
	rows = frappe.get_all(
		INCIDENT, filters={"team": c.team},
		fields=["name", "monitor", "site", "status", "started_at", "resolved_at", "duration_seconds", "cause",
		        "public_title", "hide_from_status_page"],
		order_by="started_at desc", limit_page_length=limit,
	)
	titles = dict(frappe.get_all(MONITOR, filters={"name": ["in", [r.monitor for r in rows] or [""]]}, fields=["name", "title"], as_list=True))
	for r in rows:
		r["monitor_title"] = titles.get(r.monitor) or r.monitor
		r["updates"] = frappe.get_all(
			"Status Incident Update", filters={"parent": r.name, "parenttype": INCIDENT},
			fields=["status", "message", "posted_at", "posted_by"], order_by="posted_at desc",
		)
	return rows


@frappe.whitelist(methods=["POST"])
def update_incident(name, public_title=None, hide_from_status_page=None):
	doc = _incident(name, "write")
	if public_title is not None:
		title = " ".join(str(public_title).split())
		if len(title) > 140:
			frappe.throw(_("Title is too long"))
		doc.public_title = title
	if hide_from_status_page is not None:
		doc.hide_from_status_page = 1 if str(hide_from_status_page).lower() in ("1", "true", "yes", "on") else 0
	doc.flags.ignore_permissions = True
	doc.save()
	_log(doc.team, "incident.update", doc.name, {"public_title": doc.public_title, "hidden": doc.hide_from_status_page}, INCIDENT)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def add_incident_update(name, update):
	doc = _incident(name, "write")
	try:
		clean = mu.validate_incident_update(_parse(update, _("update")))
	except mu.MonitorError as exc:
		_throw(exc)
	if clean["title"]:
		doc.public_title = clean["title"]
	doc.append("updates", {
		"status": clean["status"], "message": clean["message"], "posted_at": now_datetime(), "posted_by": frappe.session.user,
	})
	doc.flags.ignore_permissions = True
	doc.save()
	_log(doc.team, "incident.post_update", doc.name, {"status": clean["status"]}, INCIDENT)
	return {"ok": True}


# --------------------------------------------------------------------------- status page


def _status_url(page):
	return get_url(f"/status/{page.slug}")


def _status_page_view(team):
	name = frappe.db.get_value(STATUS_PAGE, {"team": team}, "name")
	if not name:
		title = frappe.db.get_value("Team", team, "title") or team
		return {"exists": False, "title": f"{title} status", "slug": mu.suggest_slug(title), "enabled": True,
		        "show_incident_history": True, "description": "", "status_domain": "", "monitors": [], "url": None}
	page = frappe.get_doc(STATUS_PAGE, name)
	return {
		"exists": True, "name": page.name, "title": page.title, "slug": page.slug, "enabled": bool(page.enabled),
		"show_incident_history": bool(page.show_incident_history), "description": page.description or "",
		"status_domain": page.status_domain or "",
		"monitors": [{"monitor": r.monitor, "display_name": r.display_name or ""} for r in page.monitors],
		"url": _status_url(page),
		"domain_url": f"https://{page.status_domain}" if page.status_domain else None,
	}


@frappe.whitelist()
def get_status_page():
	c = _ctx("read")
	return _status_page_view(c.team)


@frappe.whitelist(methods=["POST"])
def save_status_page(page):
	c = _ctx("write")
	data = _parse(page, _("status page"))
	team_monitors = frappe.get_all(MONITOR, filters={"team": c.team}, pluck="name")
	try:
		clean = mu.validate_status_page(data, team_monitors)
	except mu.MonitorError as exc:
		_throw(exc)
	if frappe.db.exists(STATUS_PAGE, {"slug": clean["slug"], "team": ["!=", c.team]}):
		frappe.throw(_("This slug is already taken"))
	name = frappe.db.get_value(STATUS_PAGE, {"team": c.team}, "name")
	doc = frappe.get_doc(STATUS_PAGE, name) if name else frappe.new_doc(STATUS_PAGE)
	doc.team = c.team
	rows = clean.pop("monitors")
	doc.update(clean)
	doc.set("monitors", [])
	for row in rows:
		doc.append("monitors", row)
	doc.flags.ignore_permissions = True
	doc.save()
	mon.clear_status_cache(doc.name)
	_log(c.team, "status_page.update" if name else "status_page.create", doc.name,
	     {"slug": doc.slug, "enabled": doc.enabled, "monitors": len(rows)}, STATUS_PAGE)
	return _status_page_view(c.team)
