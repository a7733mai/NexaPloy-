/* Nexaploy portal: agent version vs the latest Agent Release, "Upgrade agent" / "Roll back".
 *
 * Served as /assets/nexaploy/js/agent_upgrade.js and loaded by www/dashboard/server.html as a
 * plain <script> so it registers before Alpine starts. Registers `nxAgentUpgrade(server)`.
 * API: nexaploy.api.agent_releases.* (customers may upgrade their own BYOS servers only
 * when the platform allows it; the API decides and returns can_upgrade).
 */
(function () {
	"use strict";
	if (window.__nxAgentUpgradeLoaded) return;
	window.__nxAgentUpgradeLoaded = true;

	var API = "nexaploy.api.agent_releases.";

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(API + method, args); }

	function register() {
		window.Alpine.data("nxAgentUpgrade", function (server) {
			return {
				server: server,
				st: null,
				busy: false,
				timer: null,
				init: function () {
					var self = this;
					this.load();
					this.timer = setInterval(function () {
						if (self.st && (self.st.upgrade_status === "In Progress" || self.st.upgrade_status === "Rolling Back")) self.load();
					}, 15000);
				},
				destroy: function () { clearInterval(this.timer); },
				load: function () {
					var self = this;
					if (!this.server) return;
					call("server_agent_status", { server: this.server }).then(
						function (r) { self.st = r; },
						function () { self.st = null; }
					);
				},
				canUpgrade: function () {
					return !!(this.st && this.st.can_upgrade && this.st.upgrade_available && !this.st.blocker);
				},
				canRollback: function () {
					return !!(this.st && this.st.can_upgrade && this.st.has_previous);
				},
				upgrade: function () {
					var self = this;
					var msg = t("Upgrade the agent from {0} to {1}? Benches and sites keep running; the agent restarts.",
						[this.st.agent_version || "?", this.st.latest.version]);
					if (!window.confirm(msg)) return;
					this.busy = true;
					call("upgrade_agent", { server: this.server }).then(
						function () { nx().toast(t("Agent upgrade started"), "success"); self.load(); },
						function (e) { nx().toast(e.message, "error"); }
					).finally(function () { self.busy = false; });
				},
				rollback: function () {
					var self = this;
					if (!window.confirm(t("Put the previous agent version back on this server?"))) return;
					this.busy = true;
					call("rollback_agent", { server: this.server }).then(
						function () { nx().toast(t("Agent rollback started"), "success"); self.load(); },
						function (e) { nx().toast(e.message, "error"); }
					).finally(function () { self.busy = false; });
				},
			};
		});
	}

	if (window.Alpine && window.Alpine.version) {
		register();
	} else {
		document.addEventListener("alpine:init", register);
	}
})();
