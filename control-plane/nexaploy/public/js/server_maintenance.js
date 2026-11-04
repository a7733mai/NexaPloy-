/* Nexaploy portal: server maintenance & health (ARCHITECTURE.md §17).
 *
 * Loaded as a plain <script> by www/dashboard/server.html and admin.html so it registers before
 * Alpine starts. Components:
 *   nxServerMaint(server)  "System updates", "Health" and "Clean up" cards of a server
 *   nxFleetHealth          admin Overview: every server, worst first
 *   nxFleetMaint           admin Teams & servers: bulk actions + maintenance / cleanup policy
 * API: nexaploy.api.server_maintenance.* (the API decides permissions; can_manage tells the UI).
 */
(function () {
	"use strict";
	if (window.__nxServerMaintLoaded) return;
	window.__nxServerMaintLoaded = true;

	var API = "nexaploy.api.server_maintenance.";
	var ACTIVE = { Pending: 1, Queued: 1, Running: 1 };

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(API + method, args); }
	function healthClass(status) {
		return "badge " + ({ OK: "success", Warning: "warning", Critical: "danger", Offline: "warning" }[status] || "");
	}
	function sevClass(sev) { return "badge " + ({ critical: "danger", warning: "warning", info: "info" }[sev] || ""); }

	function register() {

		window.Alpine.data("nxServerMaint", function (server) {
			return {
				server: server, st: null, busy: false, timer: null,
				mode: "security", rebootPolicy: "if_required", win: {},
				dlg: false, targets: {}, cleanJob: null, cleanResult: null, cleanDry: true,
				healthClass: healthClass, sevClass: sevClass,
				init: function () {
					var self = this;
					this.load();
					this.timer = setInterval(function () {
						if (!self.st) return;
						var busy = self.st.os.os_update_status === "In Progress" || self.st.os.os_update_status === "Rebooting" ||
							self.st.health.diagnose_job || self.cleanJob;
						if (busy) self.load();
					}, 10000);
				},
				destroy: function () { clearInterval(this.timer); },
				load: function () {
					var self = this;
					call("get_maintenance", { server: this.server }).then(function (r) {
						self.st = r;
						self.win = Object.assign({}, r.maintenance);
						if (!Object.keys(self.targets).length) {
							r.cleanup.targets.forEach(function (x) { self.targets[x.target] = true; });
						}
						if (self.cleanJob) self.pollClean();
					}, function () { self.st = null; });
				},
				act: function (method, args, msg) {
					var self = this;
					this.busy = true;
					return call(method, Object.assign({ server: this.server }, args || {})).then(
						function (r) { if (msg) nx().toast(t(msg), "success"); self.load(); return r; },
						function (e) { nx().toast(e.message, "error"); throw e; }
					).finally(function () { self.busy = false; });
				},
				updateNow: function () {
					if (!window.confirm(t("Install updates on this server now?"))) return;
					this.act("update_os", { mode: this.mode, reboot: this.rebootPolicy }, "System update started");
				},
				rebootNow: function () {
					if (!window.confirm(t("Reboot this server? Sites are unavailable for a few minutes."))) return;
					this.act("reboot_server", { delay_seconds: 60 }, "Reboot scheduled");
				},
				toggleUnattended: function (on) { this.act("set_unattended", { enabled: on ? 1 : 0 }, "Saved"); },
				saveWindow: function () {
					var w = this.win;
					this.act("save_maintenance", { values: {
						maintenance_enabled: w.maintenance_enabled ? 1 : 0, maintenance_day: w.maintenance_day,
						maintenance_hour: w.maintenance_hour, maintenance_mode: w.maintenance_mode,
						maintenance_reboot: w.maintenance_reboot, auto_cleanup_enabled: w.auto_cleanup_enabled ? 1 : 0,
					} }, "Saved");
				},
				diagnose: function () { this.act("run_diagnose", {}, "Full check started"); },
				openClean: function () { this.dlg = true; this.cleanResult = null; this.cleanJob = null; },
				chosen: function () { var self = this; return Object.keys(this.targets).filter(function (k) { return self.targets[k]; }); },
				runClean: function (dry) {
					var self = this;
					this.cleanDry = dry;
					this.cleanResult = null;
					this.act("cleanup", { targets: JSON.stringify(this.chosen()), dry_run: dry ? 1 : 0 }, dry ? null : "Cleanup started")
						.then(function (r) { self.cleanJob = r.job; setTimeout(function () { self.pollClean(); }, 3000); });
				},
				pollClean: function () {
					var self = this;
					if (!this.cleanJob) return;
					call("get_job", { server: this.server, job: this.cleanJob }).then(function (r) {
						if (ACTIVE[r.status]) { setTimeout(function () { self.pollClean(); }, 4000); return; }
						self.cleanResult = r;
						self.cleanJob = null;
					});
				},
			};
		});

		window.Alpine.data("nxFleetHealth", function () {
			return {
				rows: null, counts: {}, healthClass: healthClass,
				init: function () {
					var self = this;
					call("fleet_health").then(function (r) { self.rows = r.servers; self.counts = r.counts; }, function () { self.rows = []; });
				},
			};
		});

		window.Alpine.data("nxFleetMaint", function () {
			return {
				policy: null, form: {}, busy: false, result: null,
				init: function () { this.load(); },
				load: function () {
					var self = this;
					call("get_policy").then(function (r) { self.policy = r; self.form = Object.assign({}, r.values); });
				},
				save: function () {
					var self = this;
					this.busy = true;
					call("save_policy", { values: this.form }).then(
						function (r) { self.policy = r; self.form = Object.assign({}, r.values); nx().toast(t("Saved"), "success"); },
						function (e) { nx().toast(e.message, "error"); }
					).finally(function () { self.busy = false; });
				},
				bulk: function (method, question) {
					var self = this;
					if (!window.confirm(t(question))) return;
					this.busy = true;
					call(method, { dry_run: 0 }).then(
						function (r) {
							self.result = r.servers;
							var jobs = r.servers.filter(function (s) { return s.job; }).length;
							nx().toast(t("Jobs created: {0}", [jobs]), "success");
						},
						function (e) { nx().toast(e.message, "error"); }
					).finally(function () { self.busy = false; });
				},
				updateAll: function () { this.bulk("bulk_os_update", "Install security updates (no reboot) on every server with pending updates?"); },
				cleanAll: function () { this.bulk("bulk_cleanup", "Run the cleanup policy on every active server now?"); },
			};
		});
	}

	if (window.Alpine && window.Alpine.version) {
		register();
	} else {
		document.addEventListener("alpine:init", register);
	}
})();
