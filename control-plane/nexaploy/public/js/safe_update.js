/* Nexaploy portal: "Safe update" - test an update on a copy of the site before applying it.
 *
 * Served as /assets/nexaploy/js/safe_update.js and included as a plain <script> in the head block
 * of www/dashboard/safe_update.html so Alpine.data("nxSafeUpdate") registers before Alpine starts.
 * API: nexaploy.api.update_test.* (read: overview / test; write: start, apply, delete, cancel).
 */
(function () {
	"use strict";

	var M = "nexaploy.api.update_test.";
	var POLL_MS = 4000;
	var STAGES = [
		{ key: "Preparing Bench", label: "Prepare a bench with the new version" },
		{ key: "Cloning", label: "Copy the site and run the migration" },
		{ key: "Testing", label: "Smoke check (ping, login page, database)" },
		{ key: "Passed", label: "Result" },
	];
	var BADGE = {
		Passed: "success", Applied: "success", Failed: "danger", "Apply Failed": "danger",
		Cancelled: "", Applying: "info", Pending: "info", "Preparing Bench": "info", Cloning: "info", Testing: "info",
	};

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { if (nx()) nx().toast(msg, kind); }
	function query(name) { return new URLSearchParams(location.search).get(name); }

	function factory() {
		return {
			site: null,
			loading: true,
			error: null,
			busy: false,
			ov: null,
			target: "",
			test: null,
			testName: null,
			showLog: false,
			deleteAfterApply: true,
			_timer: null,

			init: function () {
				this.site = query("site") || query("name");
				this.testName = query("test");
				var link = document.getElementById("nx-safe-update-site");
				if (link && this.site) {
					link.textContent = this.site;
					link.href = "/dashboard/site?name=" + encodeURIComponent(this.site);
				}
				if (this.site) this.load();
				else this.loading = false;
			},
			destroy: function () { clearTimeout(this._timer); },

			load: function () {
				var self = this;
				this.error = null;
				return call("get_overview", { site: this.site }).then(
					function (ov) {
						self.ov = ov;
						self.loading = false;
						var name = self.testName || ov.active_test || (ov.tests[0] && ov.tests[0].name);
						if (name) self.openTest(name);
					},
					function (e) { self.loading = false; self.error = e.message; }
				);
			},

			openTest: function (name) {
				var self = this;
				clearTimeout(this._timer);
				this.testName = name;
				return call("get_test", { name: name }).then(
					function (tst) {
						if (self.testName !== name) return;
						self.test = tst;
						if (tst.in_progress) self._timer = setTimeout(function () { self.openTest(name); }, POLL_MS);
						else if (self.ov && self.ov.active_test === name) self.refreshList();
					},
					function (e) { self.error = e.message; }
				);
			},

			refreshList: function () {
				var self = this;
				return call("get_overview", { site: this.site }).then(function (ov) { self.ov = ov; });
			},

			badge: function (status) { return "badge " + (BADGE[status] || ""); },
			stages: function () { return STAGES; },
			stageState: function (key) {
				var tst = this.test;
				if (!tst) return "";
				var order = STAGES.map(function (s) { return s.key; });
				var current = tst.status;
				if (current === "Pending") current = "Preparing Bench";
				var idx = order.indexOf(current);
				var mine = order.indexOf(key);
				if (["Passed", "Applying", "Applied", "Apply Failed"].indexOf(tst.status) !== -1) return "done";
				if (tst.status === "Failed" || tst.status === "Cancelled") {
					var failedAt = { bench: 0, clone: 1, backup: 1, stage: 1, new_site: 1, restore: 1, restore_config: 1, scrub: 1, migrate: 1, smoke_check: 2 }[tst.failed_stage];
					if (failedAt === undefined) failedAt = 3;
					if (mine < failedAt) return "done";
					return mine === failedAt ? "failed" : "";
				}
				if (key === "Preparing Bench" && !tst.created_bench && idx > 0) return "skipped";
				if (mine < idx) return "done";
				return mine === idx ? "running" : "";
			},
			stageIcon: function (key) {
				return { done: "✓", running: "…", failed: "✗", skipped: "–" }[this.stageState(key)] || "○";
			},

			_mutate: function (method, args, okMsg) {
				var self = this;
				this.busy = true;
				return call(method, args).then(
					function (r) {
						self.busy = false;
						if (okMsg) toast(okMsg, "success");
						return r;
					},
					function (e) { self.busy = false; toast(e.message, "error"); return null; }
				);
			},

			start: function () {
				var self = this;
				var msg = this.target
					? t("Copy {0} onto bench {1}, migrate it and check it? The site itself is not changed.", [this.site, this.target])
					: t("Copy {0} onto the newest version, migrate it and check it? The site itself is not changed.", [this.site]);
				if (!window.confirm(msg)) return null;
				return this._mutate("start_test", { site: this.site, target_bench: this.target || null }, t("Update test started")).then(function (r) {
					if (!r) return;
					self.refreshList();
					self.openTest(r.name);
				});
			},

			apply: function () {
				var self = this;
				if (!this.test) return null;
				var msg = t("Apply the update to {0}? A fresh backup is taken first, then the site moves to bench {1}. Expect a short downtime.", [this.site, this.test.target_bench]);
				if (!window.confirm(msg)) return null;
				return this._mutate("apply_update", { name: this.test.name, delete_test_site: this.deleteAfterApply ? 1 : 0 }, t("Applying the update")).then(function (r) {
					if (r) self.openTest(self.test.name);
				});
			},

			deleteCopy: function () {
				var self = this;
				if (!this.test || !window.confirm(t("Delete the test site {0}?", [this.test.test_site]))) return null;
				return this._mutate("delete_test_site", { name: this.test.name }, t("Deleting the test site")).then(function (r) {
					if (r) self.openTest(self.test.name);
				});
			},

			cancel: function () {
				var self = this;
				if (!this.test || !window.confirm(t("Cancel this update test?"))) return null;
				return this._mutate("cancel_test", { name: this.test.name }, t("Update test cancelled")).then(function (r) {
					if (r) { self.refreshList(); self.openTest(self.test.name); }
				});
			},

			toggleAutoTest: function (ev) {
				var self = this;
				var group = this.ov && this.ov.release_group;
				if (!group) return null;
				var enabled = ev.target.checked;
				return this._mutate("set_test_before_auto_update", { release_group: group.name, enabled: enabled ? 1 : 0 },
					enabled ? t("Automatic updates now wait for passing tests") : t("Automatic updates no longer wait for tests")).then(function (r) {
					if (r) group.test_before_auto_update = r.test_before_auto_update ? 1 : 0;
					else ev.target.checked = !enabled;
				});
			},

			fmt: function (d) { return d && nx() ? nx().fmtDate(d) : (d || "—"); },
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxSafeUpdate", factory);
	});
})();
