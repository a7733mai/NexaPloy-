"""Custom domains: DNS verification before `site.add_domain`, and TLS health (frappe side).

Pure decisions live in nexaploy/domain_utils.py; this module does the lookups, stores the
outcome on Site Domain rows (and the primary hostname's TLS fields on Site) and enqueues jobs.

DNS lookups go to public resolvers (1.1.1.1, 8.8.8.8) with dnspython - a Frappe dependency -
so a stale cache on the control plane host cannot hide a fresh record; without dnspython the
system resolver (socket) is used.

Entry points:
- verify_domain(site, domain, manual)      "Verify now" / right after Site.add_domain
- verify_pending_domains()                 scheduler, every 5 minutes (hooks.py)
- ingest_tls(server, report)               heartbeat `tls` block and site.tls_status results
- enqueue_tls_check(site)                  "Check certificate" in the portal
"""

import socket
from datetime import datetime, timezone

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from nexaploy import domain_utils as du

try:  # added by the notifications work; optional
	from nexaploy.notify import notify
except ImportError:
	notify = None

PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")
DNS_TIMEOUT = 3.0
DNS_LIFETIME = 6.0
MAX_CNAME_HOPS = 8
SCHEDULER_BATCH = 200


# --------------------------------------------------------------------------- DNS lookups


def lookup(domain):
	"""{"cname": [chain], "a": [...], "aaaa": [...], "error": str|None} for `domain`."""
	try:
		import dns.resolver  # noqa: F401
	except ImportError:
		return _lookup_socket(domain)
	return _lookup_dnspython(domain)


def _lookup_dnspython(domain):
	import dns.exception
	import dns.resolver

	resolver = dns.resolver.Resolver(configure=False)
	resolver.nameservers = list(PUBLIC_RESOLVERS)
	resolver.timeout = DNS_TIMEOUT
	resolver.lifetime = DNS_LIFETIME
	answer = {"cname": [], "a": [], "aaaa": [], "error": None}
	soft = (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer)

	name = domain
	for _hop in range(MAX_CNAME_HOPS):
		try:
			rrset = resolver.resolve(name, "CNAME")
		except soft:
			break
		except (dns.exception.DNSException, OSError) as exc:
			answer["error"] = _dns_error(exc)
			break
		target = du.normalize_host(rrset[0].target.to_text())
		if not target or target in answer["cname"]:
			break
		answer["cname"].append(target)
		name = target

	for rdtype, key in (("A", "a"), ("AAAA", "aaaa")):
		try:
			rrset = resolver.resolve(domain, rdtype)
			answer[key] = [r.to_text() for r in rrset]
		except soft:
			pass
		except (dns.exception.DNSException, OSError) as exc:
			answer["error"] = answer["error"] or _dns_error(exc)
	return answer


def _dns_error(exc):
	name = type(exc).__name__
	if "Timeout" in name:
		return "timeout"
	if name == "NoNameservers":
		return "no nameserver answered (SERVFAIL)"
	return name


def _lookup_socket(domain):
	"""Fallback without dnspython: the system resolver (may be cached)."""
	answer = {"cname": [], "a": [], "aaaa": [], "error": None}
	try:
		canonical, aliases, _ips = socket.gethostbyname_ex(domain)
		chain = [du.normalize_host(a) for a in aliases if du.normalize_host(a) != domain]
		if du.normalize_host(canonical) != domain:
			chain.append(du.normalize_host(canonical))
		answer["cname"] = chain
	except OSError:
		pass
	try:
		infos = socket.getaddrinfo(domain, None, proto=socket.IPPROTO_TCP)
	except socket.gaierror as exc:
		if exc.errno not in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)):
			answer["error"] = str(exc)
		return answer
	except OSError as exc:
		answer["error"] = str(exc)
		return answer
	for family, _t, _p, _c, sockaddr in infos:
		ip = sockaddr[0]
		key = "a" if family == socket.AF_INET else "aaaa"
		if ip not in answer[key]:
			answer[key].append(ip)
	return answer


# --------------------------------------------------------------------------- targets & instructions


def server_targets(server):
	"""IPs a custom domain must point to on `server` (see domain_utils.server_dns_targets)."""
	row = frappe.db.get_value(
		"Server", server, ["public_ipv4", "public_ipv6", "dns_target_ips", "agent_ip", "facts"], as_dict=True
	) or frappe._dict()
	facts = frappe.parse_json(row.facts) if row.facts else {}
	return du.server_dns_targets(
		row.dns_target_ips,
		row.public_ipv4,
		row.public_ipv6,
		(facts or {}).get("public_ips") if isinstance(facts, dict) else None,
		row.agent_ip,
	)


def instructions(site_name, domain, server):
	return du.dns_instructions(domain, site_name, server_targets(server))


# --------------------------------------------------------------------------- verification


def _detail(result):
	return frappe.as_json({"code": result.get("code"), "message": result["message"], "args": result["args"]})


def _row(site, domain):
	for row in site.domains:
		if row.domain == domain:
			return row
	frappe.throw(_("Domain {0} is not attached to this site").format(domain))


def _set_row(row, values):
	for key, value in values.items():
		row.set(key, value)
	frappe.db.set_value("Site Domain", row.name, values, update_modified=False)


def verify_domain(site_name, domain, manual=False, now=None):
	"""Check DNS for one domain; when correct, mark it Verifying and enqueue site.add_domain.

	manual=True ("Verify now", add_domain): a Broken domain starts over (new 72 h window).
	Active / Verifying domains are only re-checked for information (no status change, no job).
	Returns {status, ok, message, args, text, job}.
	"""
	now = now or now_datetime()
	site = frappe.get_doc("Site", site_name)
	domain = du.normalize_host(domain)
	row = _row(site, domain)
	if row.status == du.REMOVING:
		frappe.throw(_("Domain {0} is being removed").format(domain))

	informational = row.status in (du.ACTIVE, du.VERIFYING, du.LEGACY_PENDING)
	values = {}
	if row.status == du.BROKEN and manual:
		values.update({"status": du.PENDING_DNS, "dns_requested_at": now, "dns_check_attempts": 0})
	elif not row.dns_requested_at:
		values["dns_requested_at"] = now

	if row.skip_dns_check:
		result = du.skipped_result()
	else:
		targets = server_targets(site.server)
		result = du.evaluate_dns(domain, site.site_name, targets, lookup(domain))

	values.update({
		"last_checked_at": now,
		"dns_check_attempts": (row.dns_check_attempts or 0) + 1,
		"dns_check_message": result["text"],
		"dns_check_detail": _detail(result),
		"proxied": int(bool(result.get("proxied"))),
	})
	job = None
	status = values.get("status", row.status)
	if not informational and status in (du.PENDING_DNS, du.BROKEN):
		if result["ok"]:
			if site.status != "Active":
				frappe.throw(_("Site {0} is {1}; it must be Active").format(site.name, _(site.status)))
			values.update({"status": du.VERIFYING, "verification_type": result.get("verification_type") or ""})
			_set_row(row, values)
			job = site._job("site.add_domain", {"domain": domain})
			status = du.VERIFYING
		elif not manual and du.dns_gave_up(get_datetime(values.get("dns_requested_at") or row.dns_requested_at), now):
			gave_up = du.gave_up_result(domain)
			values.update({
				"status": du.BROKEN,
				"dns_check_message": gave_up["text"] + "\n" + result["text"],
				"dns_check_detail": _detail(gave_up),
			})
			_set_row(row, values)
			status = du.BROKEN
		else:
			_set_row(row, values)
	elif informational and result["ok"]:
		values["verification_type"] = result.get("verification_type") or row.verification_type or ""
		_set_row(row, values)
	else:
		_set_row(row, values)
	return {
		"status": status,
		"ok": bool(result["ok"]),
		"message": result["message"],
		"args": result["args"],
		"text": result["text"],
		"proxied": bool(result.get("proxied")),
		"job": job,
	}


def verify_pending_domains():
	"""Scheduler (every 5 minutes): re-check Pending DNS domains on a back-off schedule
	(domain_utils.check_interval) and give up after 72 hours (status Broken)."""
	rows = frappe.get_all(
		"Site Domain",
		filters={"parenttype": "Site", "status": du.PENDING_DNS},
		fields=["name", "parent", "domain", "dns_requested_at", "last_checked_at"],
		order_by="last_checked_at asc",
		limit_page_length=SCHEDULER_BATCH,
	)
	if not rows:
		return
	now = now_datetime()
	active = set(
		frappe.get_all(
			"Site", filters={"name": ["in", list({r.parent for r in rows})], "status": "Active"}, pluck="name"
		)
	)
	for r in rows:
		if r.parent not in active:
			continue
		requested = get_datetime(r.dns_requested_at) if r.dns_requested_at else None
		last = get_datetime(r.last_checked_at) if r.last_checked_at else None
		if not (du.dns_gave_up(requested, now) or du.dns_check_due(requested, last, now)):
			continue
		try:
			verify_domain(r.parent, r.domain, manual=False, now=now)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Nexaploy: DNS check failed for {r.domain}")


def on_add_domain_finished(site, domain, ok, error=None):
	"""site.add_domain callback (agent_callbacks.on_site_domain) - row values to apply."""
	for row in site.domains:
		if row.domain != domain:
			continue
		if ok:
			row.status = du.ACTIVE
			row.activated_at = now_datetime()
			row.tls_status = "Pending"
			row.tls_message = du.render(du.MSG["tls_pending"], [domain])
		else:
			row.status = du.BROKEN
			result = du.agent_failed_result(domain, error)
			row.dns_check_message = result["text"]
			row.dns_check_detail = _detail(result)


# --------------------------------------------------------------------------- TLS


def enqueue_tls_check(site_name):
	"""site.tls_status for one site (primary + custom domains). Returns the Agent Job name."""
	from nexaploy.agent_job import create_agent_job

	site = frappe.db.get_value("Site", site_name, ["name", "site_name", "server"], as_dict=True)
	running = frappe.db.get_value(
		"Agent Job",
		{
			"kind": "site.tls_status",
			"reference_doctype": "Site",
			"reference_name": site.name,
			"status": ["in", ["Pending", "Queued", "Running"]],
		},
		"name",
	)
	if running:
		return running
	return create_agent_job(site.server, "site.tls_status", {"site": site.site_name}, "Site", site.name).name


def ingest_tls(server, report):
	"""Store the agent's TLS report ({hosts: {host: info}}) on Site / Site Domain. Never raises."""
	try:
		_ingest_tls(server, report)
	except Exception:
		frappe.log_error(title=f"Nexaploy: TLS report from {server} failed")


def _ingest_tls(server, report):
	report = frappe.parse_json(report) if isinstance(report, str) else report
	hosts = (report or {}).get("hosts") if isinstance(report, dict) else None
	if not hosts or not isinstance(hosts, dict):
		return
	hosts = {du.normalize_host(h): info for h, info in hosts.items() if isinstance(info, dict)}
	checked_at = _system_time(du.parse_utc((report or {}).get("collected_at"))) or now_datetime()
	now_local = now_datetime()
	now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

	sites = frappe.get_all(
		"Site",
		filters={"server": server, "status": ["not in", ["Archived"]]},
		fields=["name", "site_name", "team", "creation", "tls_alert_marker"],
	)
	for s in sites:
		primary = du.normalize_host(s.site_name)
		if primary in hosts:
			ev = du.evaluate_tls(primary, hosts[primary], now_utc, get_datetime(s.creation), now_local)
			if ev:
				values = _tls_values(ev, checked_at)
				marker = _expiry_marker(s, primary, ev, s.tls_alert_marker)
				if marker is not None:
					values["tls_alert_marker"] = marker
				frappe.db.set_value("Site", s.name, values, update_modified=False)
		rows = frappe.get_all(
			"Site Domain",
			filters={"parenttype": "Site", "parent": s.name, "status": ["in", [du.ACTIVE, du.VERIFYING, du.LEGACY_PENDING]]},
			fields=["name", "domain", "activated_at", "tls_alert_marker"],
		)
		for row in rows:
			host = du.normalize_host(row.domain)
			if host not in hosts:
				continue
			since = get_datetime(row.activated_at) if row.activated_at else None
			ev = du.evaluate_tls(host, hosts[host], now_utc, since, now_local)
			if not ev:
				continue
			values = _tls_values(ev, checked_at)
			marker = _expiry_marker(s, host, ev, row.tls_alert_marker)
			if marker is not None:
				values["tls_alert_marker"] = marker
			frappe.db.set_value("Site Domain", row.name, values, update_modified=False)


def _system_time(dt_utc):
	if dt_utc is None:
		return None
	try:
		from frappe.utils.data import convert_utc_to_system_timezone

		return convert_utc_to_system_timezone(dt_utc).replace(tzinfo=None)
	except Exception:
		return dt_utc


def _tls_values(ev, checked_at):
	return {
		"tls_status": ev["status"],
		"tls_issuer": (ev.get("issuer") or "")[:140] or None,
		"tls_expires_on": _system_time(ev.get("expires_on")),
		"tls_checked_at": checked_at,
		"tls_message": ev["text"],
		"tls_detail": frappe.as_json({"code": ev.get("code"), "message": ev["message"], "args": ev["args"],
		                               "days_left": ev.get("days_left")}),
	}


def _expiry_marker(site, host, ev, stored_marker):
	"""New tls_alert_marker value (and fire "ssl_expiring") or None when unchanged."""
	days_left = ev.get("days_left")
	if ev["status"] not in ("Expiring", "Expired"):
		if du.reset_expiry_alert(days_left) and (stored_marker or 0):
			return 0  # renewed: re-arm
		return None
	threshold = du.expiry_alert_threshold(days_left, du.marker_to_threshold(stored_marker))
	if threshold is None:
		return None
	_notify_ssl_expiring(site, host, ev)
	return du.threshold_to_marker(threshold)


def _notify_ssl_expiring(site, host, ev):
	"""Hook point for the "ssl_expiring" event (nexaploy.notify, when installed)."""
	if notify is None or not site.team:
		return
	expires = ev.get("expires_on")
	try:
		notify(
			site.team,
			"ssl_expiring",
			_("TLS certificate for {0} expires soon").format(host),
			ev["text"],
			context={
				"site": site.name,
				"host": host,
				"status": ev["status"],
				"days_left": ev.get("days_left"),
				"expires_on": expires.isoformat() if expires else None,
				"issuer": ev.get("issuer"),
			},
			url=f"/dashboard/site?name={site.name}#domains",
		)
	except Exception:
		frappe.log_error(title=f"Nexaploy: ssl_expiring notification failed for {host}")
