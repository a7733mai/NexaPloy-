# Copyright (c) 2026, Nexaploy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from nexaploy import lifecycle_utils as lu
from nexaploy.agent_job import create_agent_job
from nexaploy.utils import is_valid_domain, is_valid_site_name, secret_ref


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

		A staging site (inserted by nexaploy.api.site_lifecycle.create_staging with
		`flags.clone_from`) is cloned from its production site instead (site.clone).
		"""
		if self.flags.clone_from:
			self.enqueue_clone(self.flags.clone_from)
			return
		if self.flags.restore_params:
			# restored from a backup as a new site (nexaploy.api.backup_destinations.restore_backup):
			# the agent creates the empty site itself, then restores into it
			params = dict(self.flags.restore_params)
			params.update({"site": self.site_name, "bench": frappe.db.get_value("Bench", self.bench, "bench_name")})
			create_agent_job(self.server, "site.restore", params, self.doctype, self.name)
			return
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
		rows = frappe.get_all(
			"Release Group App",
			filters={"parent": group, "parenttype": "Release Group"},
			fields=["app", "app_name"],
		)
		allowed = {r.app_name or r.app for r in rows}
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
		if self.status in ("Moving", "Renaming", "Archived"):
			frappe.throw(_("Cannot drop a site in status {0}").format(self.status))
		return self._job("site.drop", {"skip_backup": bool(frappe.parse_json(skip_backup))})

	# ------------------------------------------------------------------ move to another bench

	@frappe.whitelist()
	def move_to_bench(self, target_bench, allow_upgrade=False):
		"""Move this site to another bench - on this server or another one, same Frappe
		major, or a higher one with `allow_upgrade` (ARCHITECTURE.md §1: never in place).

		A chain of agent jobs driven by callbacks (nexaploy.agent_callbacks), planned by
		nexaploy.lifecycle_utils.plan_move:

		- other server: site.backup (uploaded) -> site.restore {bench, remote_path} on the
		  target (creates the site, restores files + encryption key, migrates, writes the
		  route) -> site.drop {skip_backup} on the source;
		- same server: site.backup -> site.drop (source) -> site.restore {bench, backup_id}
		  (a site name can only live in one bench per server).

		The plan is stored in move_steps / move_step_idx / move_options (status Moving);
		each successful step enqueues the next one (move_step_finished). A failure before
		the source was dropped aborts the move (site stays Active where it was); a later
		failure leaves the site Broken, with the backup in move_backup. After a successful
		move the custom domains are re-added on the target (site.add_domain).
		"""
		self._ensure_active()
		allow_upgrade = bool(frappe.parse_json(allow_upgrade) if isinstance(allow_upgrade, str) else allow_upgrade)
		if self.is_staging:
			frappe.throw(_("Staging sites stay on the bench of their production site; drop and recreate it instead"))
		if target_bench == self.bench:
			frappe.throw(_("Site is already on bench {0}").format(target_bench))
		target = frappe.db.get_value(
			"Bench", target_bench, ["name", "bench_name", "status", "server", "team", "release_group"], as_dict=True
		)
		if not target or target.status != "Active":
			frappe.throw(_("Target bench {0} is not active").format(target_bench))
		server = frappe.db.get_value("Server", target.server, ["status", "team", "ownership"], as_dict=True) or frappe._dict()
		if server.status not in ("Active", "Offline"):
			frappe.throw(_("Server {0} of the target bench is not active").format(target.server))
		if "System Manager" not in frappe.get_roles() and not (
			target.team == self.team
			or (server.team and server.team == self.team)
			or lu.is_shared_hosted(target.team, server.team, server.ownership)
		):
			raise frappe.PermissionError(_("Target bench belongs to another team"))

		source_group = frappe.db.get_value("Bench", self.bench, "release_group")
		compat = lu.move_compatibility(
			frappe.db.get_value("Release Group", source_group, "frappe_major"),
			frappe.db.get_value("Release Group", target.release_group, "frappe_major"),
			allow_upgrade,
		)
		if not compat["ok"]:
			frappe.throw(_(compat["reason"]))
		self.validate_apps_in_release_group(
			target_bench, [row.app for row in self.apps if row.status == "Installed"]
		)

		same_server = target.server == self.server
		if not same_server and not self.backup_storage_configured():
			frappe.throw(
				_("Moving a site to another server needs backup storage (Nexaploy Settings > Backup bucket): "
				  "the target server restores the backup from the bucket.")
			)

		steps = lu.plan_move(same_server)
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
				"move_options": frappe.as_json(
					{
						"allow_upgrade": allow_upgrade,
						"upgrade": compat["upgrade"],
						"same_server": same_server,
						"source_bench": self.bench,
						"source_server": self.server,
					}
				),
			}
		)
		backup.insert(ignore_permissions=True)  # enqueues site.backup (step 0)
		self.db_set("move_backup", backup.name)
		return {"backup": backup.name, "steps": steps, "same_server": same_server, "upgrade": compat["upgrade"]}

	@staticmethod
	def backup_storage_configured():
		try:
			return bool(frappe.db.get_single_value("Nexaploy Settings", "backup_bucket"))
		except Exception:
			return False

	def _move_options(self):
		return frappe.parse_json(self.move_options or "{}") or {}

	def move_step_finished(self, job):
		"""Callback hook: the current move step (`job`) reached a terminal status."""
		steps = frappe.parse_json(self.move_steps or "[]")
		idx = self.move_step_idx or 0
		if job.status != "Success":
			# nothing has changed on the source until it is dropped -> abort cleanly
			self._finish_move(success=False, abort=not lu.move_source_dropped(steps, idx), job=job)
			return
		if idx < len(steps) and steps[idx] == "site.backup":
			result = frappe.parse_json(job.result or "{}") or {}
			opts = self._move_options()
			opts["backup_id"] = result.get("backup_id") if isinstance(result, dict) else None
			self.db_set("move_options", frappe.as_json(opts))
		idx += 1
		self.db_set("move_step_idx", idx)
		if idx >= len(steps):
			self._finish_move(success=True)
			return
		self._enqueue_move_step(steps[idx])

	def _enqueue_move_step(self, kind):
		target = frappe.db.get_value("Bench", self.move_target_bench, ["server", "bench_name"], as_dict=True)
		opts = self._move_options()
		if kind == "site.drop":
			# drop on the source bench; the move backup was just taken
			self._job("site.drop", {"skip_backup": True}, server=opts.get("source_server") or self.server)
		elif kind == "site.create":
			# moves planned before the lifecycle work (create + restore {remote_path})
			create_agent_job(
				target.server, "site.create", self._create_params(self.move_target_bench), self.doctype, self.name
			)
		elif kind == "site.restore":
			remote_path = frappe.db.get_value("Site Backup", self.move_backup, "remote_path")
			steps = frappe.parse_json(self.move_steps or "[]")
			if "site.create" in steps:  # legacy plan: the site already exists on the target
				if not remote_path:
					self._note(_("Move backup {0} has no remote copy").format(self.move_backup))
					self._finish_move(success=False)
					return
				self._job("site.restore", {"remote_path": remote_path}, server=target.server)
				return
			same_server = bool(opts.get("same_server"))
			try:
				params = lu.move_restore_params(
					self.site_name, target.bench_name, same_server, opts.get("backup_id"), remote_path
				)
			except ValueError as exc:
				self._note(str(exc))
				self._finish_move(success=False, abort=not lu.move_source_dropped(steps, self.move_step_idx or 0))
				return
			create_agent_job(target.server, "site.restore", params, self.doctype, self.name)
		else:
			frappe.throw(_("Unexpected move step {0}").format(kind))

	def _finish_move(self, success, abort=False, job=None):
		values = {"move_steps": None, "move_step_idx": 0, "move_target_bench": None, "move_options": None}
		if not success and not abort:
			# Keep the move state: retrying the failed Agent Job (desk "Retry") resumes the move
			# at this step (see agent_callbacks._in_move) instead of leaving the site half-moved.
			values = {}
		if success:
			target = frappe.db.get_value("Bench", self.move_target_bench, ["name", "server"], as_dict=True)
			values.update({"bench": target.name, "server": target.server, "status": "Active"})
		else:
			values["status"] = "Active" if abort else "Broken"
			error = (job.error if job else None) or ""
			if abort:
				self._note(_("Move aborted; the site keeps running on bench {0}. {1}").format(self.bench, error))
			else:
				self._note(
					_("Move failed after the source site was removed. Retry the failed job, or restore backup {0} to recover. {1}").format(
						self.move_backup, error
					)
				)
		self.db_set(values)
		if success:
			self._readd_domains()

	def _readd_domains(self):
		"""The restored site_config on the target has no custom domains: add them again."""
		self.reload()
		rows = [row for row in self.domains if row.status != "Removing"]
		for row in rows:
			row.status = "Pending"
		if rows:
			self.flags.ignore_permissions = True
			self.save()
		for row in rows:
			self._job("site.add_domain", {"domain": row.domain})

	def _note(self, text):
		try:
			self.add_comment("Comment", text)
		except Exception:
			frappe.log_error(title=f"Nexaploy: {self.name}", message=text)

	# ------------------------------------------------------------------ lifecycle: staging & rename

	def enqueue_clone(self, source):
		"""site.clone {bench, site: source, target_site: self} on this (staging) site's server."""
		bench_name = frappe.db.get_value("Bench", self.bench, "bench_name")
		source_name = frappe.db.get_value("Site", source, "site_name") or source
		return create_agent_job(
			self.server,
			"site.clone",
			lu.clone_params(bench_name, source_name, self.site_name, scrub=True),
			reference_doctype=self.doctype,
			reference_name=self.name,
		).name

	def clone_finished(self, job):
		"""Callback for site.clone on this staging site."""
		ok = job.status == "Success"
		self.status = "Active" if ok else "Broken"
		for row in self.apps:
			if row.status == "Pending":
				row.status = "Installed" if ok else "Failed"
		if not ok:
			self._note(_("Staging copy failed: {0}").format(job.error or ""))
		self.flags.ignore_permissions = True
		self.save()

	def rename_finished(self, job):
		"""Callback for site.rename: rename the document, keep the old name as a domain."""
		params = frappe.parse_json(job.params or "{}") or {}
		new_name = params.get("new_name")
		if job.status != "Success" or not new_name:
			failed = lu.failed_step_index(
				[{"step_idx": s.step_idx, "status": s.status} for s in (job.get("steps") or [])]
			)
			# the check step changes nothing on the server
			self.db_set("status", "Active" if failed == 0 else "Broken")
			self._note(_("Rename to {0} failed: {1}").format(new_name, job.error or ""))
			return
		old_name = self.name
		if old_name != new_name and not frappe.db.exists("Site", new_name):
			# frappe.rename_doc() (the public wrapper) has no ignore_permissions in v15; the
			# model function does, and the callback runs without a customer session.
			from frappe.model.rename_doc import rename_doc

			rename_doc("Site", old_name, new_name, force=True, ignore_permissions=True, show_alert=False)
		doc = frappe.get_doc("Site", new_name)
		doc.site_name = new_name
		keep_old = params.get("keep_old_domain", True)
		domains = [row for row in doc.domains if row.domain not in (new_name, old_name)]
		doc.set("domains", [])
		for row in domains:
			doc.append("domains", {"domain": row.domain, "status": row.status})
		if keep_old:
			doc.append("domains", {"domain": old_name, "status": "Active"})
		doc.status = "Active"
		doc.flags.ignore_permissions = True
		doc.save()
		return doc
