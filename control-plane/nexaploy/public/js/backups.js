/* Nexaploy portal: backups (/dashboard/backups) - destinations (S3, Google Drive, OneDrive,
 * Dropbox with OAuth "Connect"), which servers use them, retention, restore.
 *
 * Served as /assets/nexaploy/js/backups.js and included as a plain <script> in the head block of
 * www/dashboard/backups.html so Alpine.data("nxBackups") registers before Alpine starts.
 * API: nexaploy.api.backup_destinations.* (destinations: Owner/Admin; retention, restore: write).
 */
(function () {
	"use strict";

	var M = "nexaploy.api.backup_destinations.";
	var PROVIDER = { "Google Drive": "google", OneDrive: "microsoft", Dropbox: "dropbox" };
	var STATUS = { Connected: "success", "Not Connected": "warning", Error: "danger" };
	var OAUTH_NOTICES = {
		invalid_state: ["The sign-in link is invalid. Start again from this page.", "danger"],
		expired: ["The sign-in took too long. Please connect again.", "warning"],
		forbidden: ["Only team Owners and Admins can connect backup storage.", "danger"],
		missing: ["The backup destination no longer exists.", "danger"],
		denied: ["The connection was cancelled at the provider.", "warning"],
		failed: ["Connecting failed. See the destination's error and try again.", "danger"],
	};
	var POLICY_KEYS = ["retention_days", "local_retention_days", "keep_daily", "keep_weekly", "keep_monthly"];

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { if (nx()) nx().toast(msg, kind); }
	function query(name) { return new URLSearchParams(location.search).get(name); }
	function copyPolicy(p) {
		var out = {};
		POLICY_KEYS.forEach(function (k) { out[k] = p && p[k] !== undefined ? p[k] : 0; });
		return out;
	}
	function emptyForm() {
		return {
			open: false, name: null, destination_type: "S3", title: "", folder: "", is_default: false,
			root_folder_id: "", s3_provider: "", s3_endpoint: "", s3_region: "", s3_bucket: "",
			s3_access_key: "", s3_secret_key: "",
		};
	}

	function factory() {
		return {
			loading: true,
			busy: false,
			error: null,
			notice: null,
			data: null,
			form: emptyForm(),
			teamPolicy: copyPolicy(null),
			restore: { site: null, loading: false, backups: [], target: null, newName: "", bench: "", benches: [] },

			init: function () {
				var oauth = query("oauth");
				if (query("connected")) this.notice = { kind: "success", text: t("Connected. Your servers will use it for the next backups.") };
				else if (oauth && OAUTH_NOTICES[oauth]) this.notice = { kind: OAUTH_NOTICES[oauth][1], text: t(OAUTH_NOTICES[oauth][0]) };
				if (query("connected") || oauth) history.replaceState(null, "", location.pathname);
				this.load();
			},

			load: function () {
				var self = this;
				this.loading = !this.data;
				return call("overview").then(function (r) {
					r.sites.forEach(function (s) {
						s.customSel = s.custom ? "1" : "0";
						s.edit = copyPolicy(s.policy);
					});
					self.data = r;
					self.teamPolicy = copyPolicy(r.retention.team);
					self.error = null;
				}).catch(function (e) { self.error = e.message; }).finally(function () { self.loading = false; });
			},

			canAdmin: function () { return !!(this.data && this.data.can_admin); },
			canWrite: function () { return !!(this.data && this.data.can_write); },
			statusClass: function (s) { return STATUS[s] || ""; },
			providerReady: function (type) { return !!(this.data && this.data.providers[PROVIDER[type]]); },
			usableDestinations: function () { return this.data ? this.data.destinations.filter(function (d) { return d.usable; }) : []; },
			quota: function (d) { return nx().bytes(d.quota_used || 0) + " / " + nx().bytes(d.quota_total); },

			defaultLabel: function () {
				var d = this.data && this.data.destinations.filter(function (x) { return x.is_default && x.usable; })[0];
				if (d) return t("Team default ({0})", [d.title]);
				return this.data && this.data.platform_storage ? t("Platform storage") : t("Not configured");
			},

			limitsText: function () {
				if (!this.data) return "";
				var l = this.data.retention.limits, parts = [];
				if (l.backup_retention_min_days > 1) parts.push(t("at least {0} days", [l.backup_retention_min_days]));
				if (l.backup_retention_max_days) parts.push(t("at most {0} days", [l.backup_retention_max_days]));
				if (l.backup_local_retention_max_days) parts.push(t("at most {0} days on the server", [l.backup_local_retention_max_days]));
				if (l.backup_keep_monthly_max) parts.push(t("at most {0} monthly backups", [l.backup_keep_monthly_max]));
				if (l.backup_retention_included_days && !this.data.retention.addon_active) {
					parts.push(t("longer than {0} days needs the extended retention add-on", [l.backup_retention_included_days]));
				}
				return parts.length ? t("Limits: {0}", [parts.join("; ")]) : "";
			},

			run: function (promise, ok) {
				var self = this;
				this.busy = true;
				return promise.then(function (r) {
					if (ok) toast(ok, "success");
					return r;
				}).catch(function (e) { toast(e.message, "error"); throw e; }).finally(function () { self.busy = false; });
			},

			// ---- destinations
			openForm: function (d) {
				var f = emptyForm();
				f.open = true;
				if (d) {
					Object.keys(f).forEach(function (k) { if (d[k] !== undefined && d[k] !== null && k !== "s3_secret_key") f[k] = d[k]; });
					f.is_default = !!d.is_default;
					f.open = true;
				}
				this.form = f;
			},

			save: function () {
				var self = this, f = this.form;
				var args = {};
				Object.keys(f).forEach(function (k) { if (k !== "open" && f[k] !== null && f[k] !== "") args[k] = f[k]; });
				args.is_default = f.is_default ? 1 : 0;
				return this.run(call("save_destination", args), t("Saved")).then(function (d) {
					var isNew = !f.name;
					self.form = emptyForm();
					if (isNew && d.provider) return self.connect(d);
					return self.load();
				}, function () {});
			},

			connect: function (d) {
				var self = this;
				return this.run(call("start_oauth", { provider: d.provider, destination: d.name })).then(function (r) {
					window.location.href = r.url;
				}, function () { self.load(); });
			},

			disconnect: function (d) {
				if (!window.confirm(t("Disconnect {0}? Servers using it fall back to the platform storage.", [d.title]))) return;
				var self = this;
				return this.run(call("disconnect_destination", { name: d.name }), t("Disconnected")).then(function () { self.load(); }, function () {});
			},

			remove: function (d) {
				if (!window.confirm(t("Delete {0}? Backups already stored there are not deleted.", [d.title]))) return;
				var self = this;
				return this.run(call("delete_destination", { name: d.name }), t("Deleted")).then(function () { self.load(); }, function () {});
			},

			test: function (d) {
				var self = this;
				return this.run(call("test_destination", { name: d.name }), t("Connection test started")).then(function (r) {
					nx().pollJob(r.job).done.then(function (job) {
						if (job && job.status === "Success") toast(t("{0} is reachable and writable", [d.title]), "success");
						else if (job) toast(t("Connection test failed: {0}", [job.error || ""]), "error");
						self.load();
					});
				}, function () {});
			},

			setServer: function (s, value) {
				var self = this;
				return this.run(call("set_server_destination", { server: s.name, destination: value || "" }),
					t("Backup destination updated")).then(function () { self.load(); }, function () { self.load(); });
			},

			// ---- retention
			saveTeamRetention: function () {
				var self = this;
				return this.run(call("set_team_retention", { policy: this.teamPolicy }), t("Saved")).then(function () { self.load(); }, function () {});
			},

			saveSiteRetention: function (s) {
				var self = this;
				var custom = s.customSel === "1" ? 1 : 0;
				return this.run(call("set_site_retention", { site: s.name, custom: custom, policy: custom ? s.edit : null }), t("Saved"))
					.then(function () { self.load(); }, function () {});
			},

			// ---- restore
			openRestore: function (s) {
				var self = this;
				this.restore = { site: s.name, loading: true, backups: [], target: null, newName: "", bench: "", benches: [] };
				call("list_site_backups", { site: s.name }).then(function (rows) {
					rows.forEach(function (b) { b.mode = "all"; });
					self.restore.backups = rows;
				}).catch(function (e) { toast(e.message, "error"); }).finally(function () { self.restore.loading = false; });
			},

			restoreHere: function (b) {
				var what = { all: t("Database and files"), database: t("Database only"), files: t("Files only") }[b.mode];
				if (!window.confirm(t("Restore {0} into {1}? Current data is replaced ({2}).", [nx().fmtDate(b.created_at), this.restore.site, what]))) return;
				var self = this;
				return this.run(call("restore_backup", { backup: b.name, mode: b.mode }), t("Restore started")).then(function (r) {
					window.location.href = "/dashboard/site?name=" + encodeURIComponent(r.site);
				}, function () { self.busy = false; });
			},

			restoreNew: function (b) {
				var self = this;
				this.restore.target = b;
				if (this.restore.benches.length) return;
				nx().api("list_benches").then(function (rows) {
					self.restore.benches = rows;
					if (rows.length) self.restore.bench = rows[0].name;
				});
			},

			submitNew: function () {
				var r = this.restore;
				return this.run(call("restore_backup", { backup: r.target.name, mode: r.target.mode, new_site: r.newName, bench: r.bench }),
					t("Restore started")).then(function (res) {
					window.location.href = "/dashboard/site?name=" + encodeURIComponent(res.site);
				}, function () {});
			},
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxBackups", factory);
	});
})();
