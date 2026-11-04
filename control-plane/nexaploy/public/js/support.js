/* Nexaploy portal: support tickets (list, open, conversation, reply, close, reopen).
 *
 * Served as /assets/nexaploy/js/support.js and included as a plain <script> in the head block of
 * www/dashboard/support.html so Alpine.data("nxSupport") registers before Alpine starts.
 * API: nexaploy.api.support.* (any team member). Reply HTML is sanitised by the server.
 */
(function () {
	"use strict";

	var M = "nexaploy.api.support.";
	var STATUS = { Open: "info", "Waiting on Customer": "warning", "Waiting on Support": "info", Resolved: "success", Closed: "" };
	var PRIORITY = { Low: "", Normal: "plain", High: "warning", Urgent: "danger" };

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { if (nx()) nx().toast(msg, kind); }
	function query(name) { return new URLSearchParams(location.search).get(name); }

	function factory() {
		return {
			loading: true,
			busy: false,
			error: null,
			filter: "active",
			statuses: [],
			tickets: [],
			current: null,
			opts: null,
			showNew: false,
			form: { subject: "", priority: "Normal", site: "", server: "", message: "" },
			replyText: "",

			init: function () {
				var self = this;
				var name = query("name");
				if (name) this.open(name, true);
				else this.load();
				if (query("new")) this.openNew(query("site"));
				window.addEventListener("popstate", function () {
					var n = query("name");
					if (n) self.open(n, true);
					else { self.current = null; self.load(); }
				});
			},

			statusClass: function (s) { return "badge " + (STATUS[s] || ""); },
			priorityClass: function (p) { return "badge " + (PRIORITY[p] || ""); },

			load: function () {
				var self = this;
				this.loading = true;
				return call("list_tickets", { status: this.filter || null }).then(function (r) {
					self.tickets = r.tickets;
					self.statuses = r.statuses;
					self.error = null;
				}).catch(function (e) { self.error = e.message; }).finally(function () { self.loading = false; });
			},

			openNew: function (site) {
				var self = this;
				this.showNew = true;
				if (site) this.form.site = site;
				if (this.opts) return null;
				return call("options").then(function (r) { self.opts = r; })
					.catch(function (e) { toast(e.message, "error"); });
			},

			create: function () {
				var self = this;
				this.busy = true;
				return call("create_ticket", this.form).then(function (tk) {
					self.showNew = false;
					self.form = { subject: "", priority: "Normal", site: "", server: "", message: "" };
					toast(t("Ticket {0} opened", [tk.name]), "success");
					self.show(tk);
				}).catch(function (e) { toast(e.message, "error"); }).finally(function () { self.busy = false; });
			},

			show: function (tk, replace) {
				this.current = tk;
				var url = "/dashboard/support?name=" + encodeURIComponent(tk.name);
				if (location.pathname + location.search !== url) {
					if (replace) history.replaceState(null, "", url);
					else history.pushState(null, "", url);
				}
			},

			open: function (name, replace) {
				var self = this;
				this.loading = true;
				return call("get_ticket", { name: name }).then(function (tk) {
					self.error = null;
					self.show(tk, replace);
				}).catch(function (e) { self.error = e.message; }).finally(function () { self.loading = false; });
			},

			back: function () {
				this.current = null;
				history.pushState(null, "", "/dashboard/support");
				return this.load();
			},

			_update: function (method, args, message) {
				var self = this;
				this.busy = true;
				return call(method, args).then(function (tk) {
					self.current = tk;
					if (message) toast(message, "success");
					return true;
				}).catch(function (e) { toast(e.message, "error"); return false; }).finally(function () { self.busy = false; });
			},

			reply: function () {
				var self = this;
				return this._update("reply", { name: this.current.name, message: this.replyText }, t("Reply sent")).then(function (ok) {
					if (ok) self.replyText = "";
				});
			},

			close: function () {
				if (!window.confirm(t("Close this ticket?"))) return null;
				return this._update("close_ticket", { name: this.current.name }, t("Ticket closed"));
			},

			reopen: function () { return this._update("reopen_ticket", { name: this.current.name }, t("Ticket reopened")); },
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxSupport", factory);
	});
})();
