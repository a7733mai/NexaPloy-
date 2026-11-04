/* Nexaploy portal: control plane self-update + first-run setup checklist (/dashboard/admin Overview).
 *
 * Markup: templates/includes/controlplane_admin.html. API: nexaploy.api.controlplane.* (platform admins).
 * During an update the control plane restarts: polling errors are expected and only mean "restarting".
 */
(function () {
	"use strict";

	var M = "nexaploy.api.controlplane.";
	var POLL_MS = 5000;

	function nx() { return window.nx; }
	function t(msg) { return typeof window.__ === "function" ? window.__(msg) : msg; }
	function call(method, args) { return nx().call(M + method, args); }

	function factory() {
		return {
			st: null,
			setup: null,
			job: null,
			target: "",
			busy: false,
			offline: false,
			timer: null,

			init: function () {
				var self = this;
				this.load(false);
				call("get_setup_checklist", {}).then(function (r) { self.setup = r; }).catch(function () {});
			},

			destroy: function () { if (this.timer) clearTimeout(this.timer); },

			load: function (force) {
				var self = this;
				this.busy = true;
				return call("get_status", { force: force ? 1 : 0 }).then(function (r) {
					self.st = r;
					self.job = r.job;
					if (self.running()) self.poll();
				}).catch(function (e) { if (nx()) nx().toast(e.message, "danger"); })
					.finally(function () { self.busy = false; });
			},

			running: function () { return !!(this.job && !this.job.finished); },

			update: function () {
				var self = this, v = (this.target || "").trim(), args = {};
				if (v) { if (v.indexOf(":") > 0) args.image = v; else args.version = v; }
				if (!window.confirm(t("Update the control plane now? It restarts during the update."))) return;
				this.busy = true;
				call("start_update", args).then(function (job) {
					self.job = job;
					self.poll();
				}).catch(function (e) { nx().toast(e.message, "danger"); })
					.finally(function () { self.busy = false; });
			},

			poll: function () {
				var self = this;
				if (this.timer) clearTimeout(this.timer);
				this.timer = setTimeout(function () {
					call("get_update", { name: self.job.name }).then(function (job) {
						self.offline = false;
						self.job = job;
						if (job.finished) {
							nx().toast(self.stateLabel(), job.state === "updated" || job.state === "up_to_date" ? "success" : "danger");
							self.load(false);
						} else self.poll();
					}).catch(function () { self.offline = true; self.poll(); });
				}, POLL_MS);
			},

			stateLabel: function () {
				var s = this.job && this.job.state;
				return t({ pending: "Queued", running: "Updating", updated: "Updated", up_to_date: "Already up to date",
					rolled_back: "Rolled back", rollback_incomplete: "Rollback incomplete", failed: "Failed" }[s] || s || "");
			},

			stateClass: function () {
				var s = this.job && this.job.state;
				if (s === "updated" || s === "up_to_date") return "badge success";
				if (s === "running" || s === "pending") return "badge info pulse";
				return s === "rolled_back" ? "badge warning" : "badge danger";
			},

			dismiss: function () {
				var self = this;
				this.busy = true;
				call("dismiss_setup", { dismissed: 1 }).then(function (r) { self.setup = r; })
					.finally(function () { self.busy = false; });
			},
		};
	}

	document.addEventListener("alpine:init", function () { window.Alpine.data("nxControlPlane", factory); });
})();
