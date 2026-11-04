/* Nexaploy portal: managed e-mail of a site (E-mail tab of /dashboard/site).
 *
 * Served as /assets/nexaploy/js/site_email.js; included as a plain <script> in the `head` block
 * of www/dashboard/site.html so Alpine.data("nxSiteEmail") registers before Alpine starts.
 * Nested inside nxSite, so `site` and `tab` of the parent scope are visible. API:
 * nexaploy.api.site_email (get_site_email, set_managed_email, set_sender_domain,
 * check_sender_domain).
 */
(function () {
	"use strict";

	var M = "nexaploy.api.site_email.";
	var POLL_MS = 4000;
	var MAX_POLLS = 60;

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { return nx().toast(msg, kind); }

	function factory() {
		return {
			em: null,
			emLoading: false,
			emError: null,
			busy: false,
			domainInput: "",
			_timer: null,
			_polls: 0,

			siteName: function () {
				return nx() ? nx().query("name") : new URLSearchParams(location.search).get("name");
			},
			destroy: function () { clearTimeout(this._timer); },

			apply: function (r) {
				this.em = r;
				this.emError = null;
				if (document.activeElement === null || !this.$el.contains(document.activeElement)) {
					this.domainInput = r.sender_domain || "";
				}
				this.schedule();
			},

			schedule: function () {
				var self = this;
				clearTimeout(this._timer);
				var pending = this.em && (this.em.status === "Pending" || ["Pending", "Queued", "Running"].indexOf(this.em.job_status) !== -1);
				if (!pending || this._polls >= MAX_POLLS) return;
				this._polls += 1;
				this._timer = setTimeout(function () { self.load(true); }, POLL_MS);
			},

			load: function (quiet) {
				var self = this;
				var name = this.siteName();
				if (!name) return Promise.resolve();
				if (!quiet) this.emLoading = true;
				return call("get_site_email", { site: name }).then(
					function (r) { self.apply(r); },
					function (e) { self.emError = e.message; }
				).finally(function () { self.emLoading = false; });
			},

			toggle: function (enabled) {
				var self = this;
				if (!enabled && !window.confirm(t("Disable managed e-mail? The site stops sending through the platform relay."))) return;
				this.busy = true;
				this._polls = 0;
				call("set_managed_email", { site: this.siteName(), enabled: enabled ? 1 : 0 }).then(
					function (r) {
						self.apply(r);
						toast(enabled ? t("Managed e-mail is being set up") : t("Managed e-mail is being disabled"), "success");
					},
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.busy = false; });
			},

			saveDomain: function () {
				var self = this;
				this.busy = true;
				call("set_sender_domain", { site: this.siteName(), domain: this.domainInput.trim() }).then(
					function (r) { self.apply(r); toast(t("Sending domain saved"), "success"); },
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.busy = false; });
			},

			check: function () {
				var self = this;
				this.busy = true;
				this._polls = 0;
				call("check_sender_domain", { site: this.siteName() }).then(
					function (r) {
						self.apply(r);
						if (r.domain_verified) toast(t("Domain verified"), "success");
						else if (r.check && !r.check.checked) toast(t("DNS lookup unavailable, try again later"), "error");
						else toast(t("Some records are missing or wrong"), "error");
					},
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.busy = false; });
			},

			/* expected records merged with the last check result */
			rows: function () {
				if (!this.em) return [];
				var checked = (this.em.check && this.em.check.records) || [];
				return this.em.records.map(function (rec) {
					var hit = checked.filter(function (c) { return c.purpose === rec.purpose && c.host === rec.host; })[0];
					return Object.assign({}, rec, {
						checked: !!(hit && hit.message !== "DNS lookup unavailable"),
						ok: !!(hit && hit.ok),
						message: hit ? hit.message : "",
					});
				});
			},
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxSiteEmail", factory);
	});
})();
