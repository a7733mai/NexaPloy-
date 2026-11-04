/* Nexaploy portal: Storage tab of /dashboard/site (offsite attachments, DR replica, trash bin).
 *
 * Served as /assets/nexaploy/js/storage.js; included as a plain <script> in the `head` block of
 * www/dashboard/site.html so it registers before Alpine starts. Uses window.nx (dashboard.js)
 * lazily. Alpine.data("nxSiteStorage") is nested inside nxSite, so `site` and `tab` are visible.
 * API: nexaploy.api.storage (ARCHITECTURE.md §15).
 */
(function () {
	"use strict";

	var M = "nexaploy.api.storage.";

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { return nx().toast(msg, kind); }

	function factory() {
		return {
			stg: null,
			stgError: null,
			stgLoading: false,
			stgBusy: false,
			form: { attachments_mode: "Local", primary_profile: "", replica_profile: "", trash_profile: "", trash_retention_days: 30 },
			trash: { loading: false, entries: null, error: null, truncated: false },
			restoring: null,
			_stgTimer: null,

			stgInit: function () {
				if (!this.stg && !this.stgLoading) this.loadStorage();
			},
			destroy: function () { clearTimeout(this._stgTimer); },

			loadStorage: function () {
				var self = this;
				var name = nx() ? nx().query("name") : new URLSearchParams(location.search).get("name");
				this.stgLoading = true;
				return call("get_site_storage", { site: name }).then(
					function (r) {
						self.stg = r;
						self.stgError = null;
						self.resetForm();
						self.scheduleStorage();
					},
					function (e) { self.stgError = e.message; }
				).then(function () { self.stgLoading = false; });
			},

			resetForm: function () {
				var p = this.stg.policy || {};
				var d = this.stg.defaults || {};
				this.form = {
					attachments_mode: p.attachments_mode || "Local",
					primary_profile: p.primary_profile || d.primary_profile || "",
					replica_profile: p.replica_profile || "",
					trash_profile: p.trash_profile || "",
					trash_retention_days: p.trash_retention_days || this.stg.features.default_trash_retention_days || 30
				};
				if (!this.stg.policy && this.stg.features.replica) this.form.replica_profile = d.replica_profile || "";
				if (!this.stg.policy && this.stg.features.trash) this.form.trash_profile = d.trash_profile || "";
			},

			busyStatus: function () {
				var s = this.stg && this.stg.policy && this.stg.policy.status;
				return s === "Migrating" || s === "Migrating Back";
			},

			scheduleStorage: function () {
				var self = this;
				clearTimeout(this._stgTimer);
				var running = (this.stg.jobs || []).some(function (j) { return ["Pending", "Queued", "Running"].indexOf(j.status) >= 0; });
				if (this.busyStatus() || running) {
					this._stgTimer = setTimeout(function () { self.loadStorage(); }, 5000);
				}
			},

			profileLabel: function (name) {
				var p = (this.stg && this.stg.profiles || []).find(function (x) { return x.name === name; });
				if (!p) return name || "—";
				return p.title + (p.region_label ? " · " + p.region_label : "") + " (" + p.provider_label + ")";
			},

			addonPrice: function (key) {
				var a = this.stg && this.stg.addons && this.stg.addons[key];
				if (!a || !a.enabled) return "";
				return nx().money(a.price_monthly, a.currency) + " / " + t("month");
			},

			monthlyTotal: function () {
				var total = 0, currency = null, self = this;
				if (this.form.attachments_mode !== "Offloaded") return "";
				[["offload", true], ["replica", !!this.form.replica_profile], ["trash", !!this.form.trash_profile]].forEach(function (x) {
					var a = self.stg.addons[x[0]];
					if (x[1] && a && a.enabled) { total += Number(a.price_monthly || 0); currency = currency || a.currency; }
				});
				return total ? nx().money(total, currency) + " / " + t("month") : "";
			},

			dirty: function () {
				var p = (this.stg && this.stg.policy) || { attachments_mode: "Local" };
				var f = this.form;
				return f.attachments_mode !== (p.attachments_mode || "Local") ||
					(f.attachments_mode === "Offloaded" && ((f.primary_profile || "") !== (p.primary_profile || "") ||
						(f.replica_profile || "") !== (p.replica_profile || "") ||
						(f.trash_profile || "") !== (p.trash_profile || "") ||
						Number(f.trash_retention_days || 0) !== Number(p.trash_retention_days || 0)));
			},

			saveStorage: function () {
				var self = this;
				var f = this.form;
				var msg = null;
				var before = (this.stg.policy && this.stg.policy.attachments_mode) || "Local";
				if (f.attachments_mode === "Offloaded" && before !== "Offloaded") {
					msg = t("Move this site's attachments to object storage? Existing files are copied, verified and then removed from the server.");
				} else if (f.attachments_mode === "Local" && before === "Offloaded") {
					msg = t("Copy all attachments back to the server's disk and stop using object storage?");
				}
				if (msg && !window.confirm(msg)) return;
				this.stgBusy = true;
				call("update_site_storage", {
					site: this.stg.site,
					attachments_mode: f.attachments_mode,
					primary_profile: f.primary_profile || null,
					replica_profile: f.attachments_mode === "Offloaded" ? (f.replica_profile || null) : null,
					trash_profile: f.attachments_mode === "Offloaded" && f.replica_profile ? (f.trash_profile || null) : null,
					trash_retention_days: f.trash_retention_days
				}).then(
					function (r) {
						toast(r.job ? t("Storage change queued") : t("Saved"), "success");
						return self.loadStorage();
					},
					function (e) { toast(e.message, "error"); }
				).then(function () { self.stgBusy = false; });
			},

			failover: function (target) {
				var self = this;
				var msg = target === "replica"
					? t("Serve this site's files from the replica? Use this when the primary region is down. Files written since the last replication are not on the replica.")
					: t("Switch back to the primary? Files written during the failover are copied to the primary first.");
				if (!window.confirm(msg)) return;
				this.stgBusy = true;
				call("failover_site_storage", { site: this.stg.site, target: target }).then(
					function () { toast(t("Storage change queued"), "success"); return self.loadStorage(); },
					function (e) { toast(e.message, "error"); }
				).then(function () { self.stgBusy = false; });
			},

			refreshStatus: function () {
				var self = this;
				this.stgBusy = true;
				call("refresh_site_storage_status", { site: this.stg.site, with_size: 1 }).then(
					function (r) { return self.waitJob(r.job); }
				).then(
					function () { return self.loadStorage(); },
					function (e) { toast(e.message, "error"); }
				).then(function () { self.stgBusy = false; });
			},

			waitJob: function (job) {
				var self = this;
				return new Promise(function (resolve, reject) {
					var tries = 0;
					(function poll() {
						call("get_storage_job", { site: self.stg.site, job: job }).then(function (j) {
							if (j.status === "Success") return resolve(j);
							if (j.status === "Failure") return reject(new Error(j.error || t("Failure")));
							if (++tries > 120) return reject(new Error(t("Timed out waiting for the server")));
							setTimeout(poll, 2000);
						}, reject);
					})();
				});
			},

			loadTrash: function () {
				var self = this;
				this.trash = { loading: true, entries: null, error: null, truncated: false };
				call("list_site_trash", { site: this.stg.site }).then(
					function (r) { return self.waitJob(r.job); }
				).then(
					function (j) {
						self.trash.entries = (j.result && j.result.entries) || [];
						self.trash.truncated = !!(j.result && j.result.truncated);
					},
					function (e) { self.trash.error = e.message; }
				).then(function () { self.trash.loading = false; });
			},

			trashWhen: function (path) {
				var ts = String(path || "").split("/")[0];
				var m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(ts);
				if (!m) return ts;
				return nx().fmtDate(m[1] + "-" + m[2] + "-" + m[3] + "T" + m[4] + ":" + m[5] + ":" + m[6] + "Z");
			},
			trashFile: function (path) { return String(path || "").split("/").slice(1).join("/"); },

			restore: function (path) {
				var self = this;
				if (!window.confirm(t("Restore {0}?", [this.trashFile(path)]))) return;
				this.restoring = path;
				call("restore_site_trash", { site: this.stg.site, item: path }).then(
					function (r) { return self.waitJob(r.job); }
				).then(
					function () { toast(t("File restored"), "success"); },
					function (e) { toast(e.message, "error"); }
				).then(function () { self.restoring = null; });
			}
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxSiteStorage", factory);
	});
})();
