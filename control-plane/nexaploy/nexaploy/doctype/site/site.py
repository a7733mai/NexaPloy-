# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy.agent_job import create_agent_job
from nexaploy.utils import is_valid_domain, is_valid_site_name, plan_site_move, secret_ref


class Site(Document):
	# ------------------------------------------------------------------ lifecycle

	def before_insert(self):
		if not self.team:
			self.team = frappe.db.get_value("Bench", self.bench, "team") or self._current_team()
		self.status = "Pending"
		for row in self.apps:
			row.status = "Pending"
		for row in self.domains:
			row.status = "Pending"

	@staticmethod
	def _current_team():
		from nexaploy.teams import get_current_team, is_platform_admin

		if is_platform_admin():
			return None
		return get_current_team()

	def validate(self):
		if self.site_name:
			self.site_name = self.site_name.strip().lower()
		if not is_valid_site_name(self.site_name):
			frappe.throw(_("Site name must be a lowercase FQDN, e.g. erp.example.com"))
		if self.is_new():
			bench_status = frappe.db.get_value("Bench", self.bench, "status")
			if bench_status != "Active":
				frappe.throw(_("Bench {0} is not active").format(self.bench))
			bench_team = frappe.db.get_value("Bench", self.bench, "team")
			if "System Manager" not in frappe.get_roles() and bench_team and bench_team != self.team:
				raise frappe.PermissionError(_("Bench {0} belongs to another team").format(self.bench))
			self.validate_apps_in_release_group(self.bench, [row.app for row in self.apps])
		if (self.backup_retention_days or 0) < 0:
			frappe.throw(_("Backup retention must be 0 (keep forever) or more days"))
		if self.plan and frappe.db.get_value("Nexaploy Plan", self.plan, "plan_type") != "Site":
			frappe.throw(_("Plan {0} is not a Site plan").format(self.plan))
		seen = set()
		for row in self.domains:
			row.domain = (row.domain or "").strip().lower()
			if not is_valid_domain(row.domain):
				frappe.throw(_("Invalid domain {0}").format(row.domain))
			if row.domain in seen or row.domain == self.site_name:
				frappe.throw(_("Duplicate domain {0}").format(row.domain))
			seen.add(row.domain)

	def after_insert(self):
		"""Enqueue site.create {bench, name, admin_password, apps}.

		admin_password is a placeholder resolved from this Password field at poll time,
		so the plaintext is never stored in Agent Job.params.
		"""
		create_agent_job(
			self.server,
			"site.create",
			self._create_params(self.bench),
			reference_doctype=self.doctype,
			reference_name=self.name,
		)

	def _create_params(self, bench):
		return {
			"bench": frappe.db.get_value("Bench", bench, "bench_name"),
			"name": self.site_name,
			"admin_password": secret_ref(self.doctype, self.name, "admin_password"),
			"apps": [row.app for row in self.apps if row.status != "Failed"],
		}

	@staticmethod
	def validate_apps_in_release_group(bench, apps):
		group = frappe.db.get_value("Bench", bench, "release_group")
		allowed = set(frappe.get_all("Release Group App", filters={"parent": group, "parenttype": "Release Group"}, pluck="app"))
		missing = [a for a in apps if a not in allowed]
		if missing:
			frappe.throw(
				_("Apps {0} are not part of release group {1}").format(", ".join(missing), group)
			)

	def _job(self, kind, params=None, server=None):
		return create_agent_job(
			server or self.server, kind, {"site": self.site_name, **(params or {})}, self.doctype, self.name
		).name

	def _ensure_active(self):
		self.check_permission("write")
		if self.status != "Active":
			frappe.throw(_("Site {0} is {1}; it must be Active").format(self.name, self.status))

	# ------------------------------------------------------------------ whitelisted actions

	@frappe.whitelist()
	def backup(self, with_files=True):
		self.check_permission("read")
		if self.status not in ("Active", "Broken"):
			frappe.throw(_("Cannot back up a site in status {0}").format(self.status))
		doc = frappe.get_doc(
			{"doctype": "Site Backup", "site": self.name, "with_files": int(frappe.parse_json(with_files) or 0)}
		).insert(ignore_permissions=True)
		return doc.name

	@frappe.whitelist()
	def install_app(self, app):
		self._ensure_active()
		if any(row.app == app and row.status == "Installed" for row in self.apps):
			frappe.throw(_("{0} is already installed").format(app))
		self.validate_apps_in_release_group(self.bench, [app])
		self.apps = [row for row in self.apps if row.app != app]
		self.append("apps", {"app": app, "status": "Pending"})
		self.save()
		return self._job("site.install_app", {"app": app})

	@frappe.whitelist()
	def uninstall_app(self, app):
		self._ensure_active()
		if not any(row.app == app for row in self.apps):
			frappe.throw(_("{0} is not installed").format(app))
		return self._job("site.uninstall_app", {"app": app})

	@frappe.whitelist()
	def add_domain(self, domain):
		self._ensure_active()
		domain = (domain or "").strip().lower()
		if frappe.db.exists("Site Domain", {"domain": domain, "parenttype": "Site"}) or frappe.db.exists(
			"Site", domain
		):
			frappe.throw(_("Domain {0} is already in use").format(domain))
		self.append("domains", {"domain": domain, "status": "Pending"})
		self.save()  # validate() checks the FQDN rule
		return self._job("site.add_domain", {"domain": domain})

	@frappe.whitelist()
	def remove_domain(self, domain):
		self._ensure_active()
		rows = [row for row in self.domains if row.domain == domain]
		if not rows:
			frappe.throw(_("Domain {0} is not attached to this site").format(domain))
		rows[0].status = "Removing"
		self.save()
		return self._job("site.remove_domain", {"domain": domain})

	@frappe.whitelist()
	def migrate(self):
		self._ensure_active()
		return self._job("site.migrate")

	@frappe.whitelist()
	def drop(self, skip_backup=False):
		"""Drop the site on the server (the agent backs it up first unless skip_backup)."""
		self.check_permission("delete")
		if self.status in ("Moving", "Archived"):
			frappe.throw(_("Cannot drop a site in status {0}").format(self.status))
		return self._job("site.drop", {"skip_backup": bool(frappe.parse_json(skip_backup))})

	# ------------------------------------------------------------------ move to another bench

	@frappe.whitelist()
	def move_to_bench(self, target_bench):
		"""Move this site to another bench, e.g. to upgrade v14 -> v15 (ARCHITECTURE.md §1).

		Never in place - a chain of agent jobs driven by callbacks (nexaploy.agent_callbacks):

		1. site.backup on the source (Site Backup, purpose=Move, uploaded to the remote)
		2. site.create + site.restore {remote_path} on the target bench (restore migrates),
		3. site.drop {skip_backup} on the source.

		On the same server the drop happens before the create (the agent resolves a site's
		bench by directory scan, so a name can only live in one bench per server). The plan
		is stored in move_steps / move_step_idx; each successful step enqueues the next one
		(move_step_finished). A failed backup aborts the move (site stays Active on the
		source); any later failure leaves the site Broken for an operator to resolve, with
		the backup in move_backup.
		"""
		self._ensure_active()
		if target_bench == self.bench:
			frappe.throw(_("Site is already on bench {0}").format(target_bench))
		target = frappe.db.get_value("Bench", target_bench, ["status", "server", "team"], as_dict=True)
		if not target or target.status != "Active":
			frappe.throw(_("Target bench {0} is not active").format(target_bench))
		if "System Manager" not in frappe.get_roles() and target.team != self.team:
			raise frappe.PermissionError(_("Target bench belongs to another team"))
		self.validate_apps_in_release_group(
			target_bench, [row.app for row in self.apps if row.status == "Installed"]
		)

		steps = plan_site_move(same_server=(target.server == self.server))
		backup = frappe.get_doc(
			{"doctype": "Site Backup", "site": self.name, "with_files": 1, "purpose": "Move"}
		)
		# set the move state *before* the backup job exists so its callback sees it
		self.db_set(
			{
				"status": "Moving",
				"move_target_bench": target_bench,
				"move_steps": frappe.as_json(steps),
				"move_step_idx": 0,
			}
		)
		backup.insert(ignore_permissions=True)  # enqueues site.backup (step 0)
		self.db_set("move_backup", backup.name)
		return {"backup": backup.name, "steps": steps}

	def move_step_finished(self, job):
		"""Callback hook: the current move step (`job`) reached a terminal status."""
		steps = frappe.parse_json(self.move_steps or "[]")
		idx = self.move_step_idx or 0
		if job.status != "Success":
			# nothing has changed yet if the backup failed -> abort cleanly
			self._finish_move(success=False, abort=(idx == 0))
			return
		idx += 1
		self.db_set("move_step_idx", idx)
		if idx >= len(steps):
			self._finish_move(success=True)
			return
		self._enqueue_move_step(steps[idx])

	def _enqueue_move_step(self, kind):
		target_server = frappe.db.get_value("Bench", self.move_target_bench, "server")
		if kind == "site.drop":
			# drop on the source bench; backup was just taken
			self._job("site.drop", {"skip_backup": True}, server=self.server)
		elif kind == "site.create":
			create_agent_job(
				target_server, "site.create", self._create_params(self.move_target_bench), self.doctype, self.name
			)
		elif kind == "site.restore":
			remote_path = frappe.db.get_value("Site Backup", self.move_backup, "remote_path")
			if not remote_path:
				frappe.log_error(title=f"Nexaploy move: backup {self.move_backup} has no remote_path")
				self._finish_move(success=False)
				return
			self._job("site.restore", {"remote_path": remote_path}, server=target_server)
		else:
			frappe.throw(_("Unexpected move step {0}").format(kind))

	def _finish_move(self, success, abort=False):
		values = {"move_steps": None, "move_step_idx": 0, "move_target_bench": None}
		if success:
			target = frappe.db.get_value("Bench", self.move_target_bench, ["name", "server"], as_dict=True)
			values.update({"bench": target.name, "server": target.server, "status": "Active"})
		else:
			values["status"] = "Active" if abort else "Broken"
		self.db_set(values)
