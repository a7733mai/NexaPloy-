/* Nexaploy portal: console access - per-member SQL / console / logs permissions, the team's
 * "require 2FA for console" flag and the Console Audit Log (team Owners/Admins).
 *
 * Served as /assets/nexaploy/js/console_access.js and included as a plain <script> in the head block
 * of www/dashboard/console_access.html so Alpine.data("nxConsoleAccess") registers before Alpine
 * starts. API: nexaploy.api.console_access.* - every check is repeated on the server.
 */
(function () {
	"use strict";

	var M = "nexaploy.api.console_access.";
	var STATUS = { Success: "success", Error: "warning", Failed: "danger", Denied: "danger", Queued: "info", Running: "info" };

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { if (nx()) nx().toast(msg, kind); }

	function emptyFilters() { return { site: "", user: "", kind: "", status: "", date_from: "", date_to: "" }; }

	function factory() {
		return {
			loading: true,
			error: null,
			busy: false,
			access: null,
			edits: {},
			audit: { loading: false, error: null, rows: [], page: 1, hasMore: false, sites: [], users: [], statuses: [] },
			filters: emptyFilters(),
			open: {},

			init: function () {
				if (!nx() || !nx().can("admin")) { this.loading = false; return; }
				this.load();
				this.loadAudit(1);
			},

			load: function () {
				var self = this;
				return call("get_console_access").then(
					function (a) {
						self.access = a;
						self.loading = false;
						self.edits = {};
						a.members.forEach(function (m) { self.edits[m.user] = Object.assign({}, m.capabilities); });
					},
					function (e) { self.loading = false; self.error = e.message; }
				);
			},

			dirty: function (m) {
				var e = this.edits[m.user] || {};
				return Object.keys(m.capabilities).some(function (k) { return !!e[k] !== !!m.capabilities[k]; });
			},

			setCap: function (m, key, value) {
				var e = this.edits[m.user];
				e[key] = value;
				if (key === "can_run_sql_write" && value) e.can_run_sql = true;
				if (key === "can_run_sql" && !value) e.can_run_sql_write = false;
			},

			save: function (m) {
				var self = this;
				this.busy = true;
				return call("set_member_capabilities", { user: m.user, capabilities: JSON.stringify(this.edits[m.user]) }).then(
					function () { self.busy = false; toast(t("Permissions saved"), "success"); self.load(); },
					function (e) { self.busy = false; toast(e.message, "error"); }
				);
			},

			reset: function (m) {
				var self = this;
				this.busy = true;
				return call("set_member_capabilities", { user: m.user, reset: 1 }).then(
					function () { self.busy = false; toast(t("Back to the role defaults"), "success"); self.load(); },
					function (e) { self.busy = false; toast(e.message, "error"); }
				);
			},

			setTwoFactor: function (ev) {
				var self = this;
				var required = ev.target.checked;
				this.busy = true;
				return call("set_console_two_factor", { required: required ? 1 : 0 }).then(
					function (r) {
						self.busy = false;
						self.access.console_two_factor_required = r.console_two_factor_required;
						toast(required ? t("Two-factor authentication is now required for SQL and the console") : t("Two-factor authentication is no longer required for SQL and the console"), "success");
						if (required && !r.system_two_factor) toast(t("Two-factor login is not enabled on this platform yet; members without it are refused."), "warning");
					},
					function (e) { self.busy = false; ev.target.checked = !required; toast(e.message, "error"); }
				);
			},

			loadAudit: function (page) {
				var self = this;
				var a = this.audit;
				a.loading = true;
				a.error = null;
				var args = Object.assign({ page: page || 1 }, this.filters);
				return call("get_console_audit", args).then(
					function (r) {
						a.loading = false;
						a.rows = r.rows;
						a.page = r.page;
						a.hasMore = r.has_more;
						a.sites = r.sites;
						a.users = r.users;
						a.statuses = r.statuses;
					},
					function (e) { a.loading = false; a.error = e.message; }
				);
			},
			resetFilters: function () { this.filters = emptyFilters(); return this.loadAudit(1); },

			statusClass: function (s) { return "badge " + (STATUS[s] || ""); },
			duration: function (r) { return r.duration === null || r.duration === undefined ? "—" : Number(r.duration).toFixed(2) + " s"; },
			preview: function (r) {
				var text = r.command || "";
				return this.open[r.name] || text.length <= 160 ? text : text.slice(0, 160) + "…";
			},
			toggle: function (r) { this.open[r.name] = !this.open[r.name]; },
			fmt: function (d) { return d && nx() ? nx().fmtDate(d) : (d || "—"); },
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxConsoleAccess", factory);
	});
})();
