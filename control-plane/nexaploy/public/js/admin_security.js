/* Nexaploy admin area: require two-factor authentication for administrators.
 * Plain script loaded by www/dashboard/admin.html before Alpine starts; registers
 * `nxAdminSecurity`. API: nexaploy.api.admin_security.*
 */
(function () {
	"use strict";
	if (window.__nxAdminSecurityLoaded) return;
	window.__nxAdminSecurityLoaded = true;
	var API = "nexaploy.api.admin_security.";
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }

	function register() {
		window.Alpine.data("nxAdminSecurity", function () {
			return {
				tf: null, method: "OTP App", busy: false,
				init: function () { this.load(); },
				load: function () {
					var self = this;
					window.nx.call(API + "get_two_factor_status").then(function (r) { self.tf = r; self.method = r.method || "OTP App"; })
						.catch(function () { self.tf = null; });
				},
				save: function (enabled) {
					var self = this;
					if (!enabled && !window.confirm(t("Stop requiring two-factor authentication for administrators?"))) return;
					self.busy = true;
					window.nx.call(API + "require_admin_two_factor", { enabled: enabled, method: self.method })
						.then(function (r) { self.tf = r; window.nx.toast(t("Saved")); })
						.catch(function (e) { window.nx.toast((e && e.message) || t("Could not save"), "danger"); })
						.finally(function () { self.busy = false; });
				},
			};
		});
	}
	if (window.Alpine) register(); else document.addEventListener("alpine:init", register);
})();
