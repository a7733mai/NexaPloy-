/* Nexaploy portal: custom domains - DNS records to create, verification, TLS health.
 *
 * Served as /assets/nexaploy/js/domains.js; www/dashboard/site.html includes it as a plain
 * <script> in its head block so it registers before Alpine starts. Uses window.nx
 * (dashboard.js) lazily, at call time.
 *
 * Alpine.data("nxDomains") is nested inside nxSite on /dashboard/site (`site` and `tab` come
 * from the parent scope). API: nexaploy.api.domains.*
 */
(function () {
	"use strict";

	var M = "nexaploy.api.domains.";
	var TLS_CLASS = {
		Valid: "success", Pending: "info pulse", Expiring: "warning", Expired: "danger",
		"Default Certificate": "danger", Untrusted: "warning", Error: "warning",
	};
	var DOMAIN_CLASS = {
		"Pending DNS": "warning", Verifying: "info pulse", Pending: "info pulse", Active: "success",
		Removing: "info pulse", Broken: "danger",
	};

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { return nx().toast(msg, kind); }

	function factory() {
		return {
			dn: null,
			dnLoading: false,
			dnError: null,
			newDomain: "",
			skipNew: false,
			preview: null,
			dnBusy: {},
			_dnTimer: null,

			init: function () {
				var self = this;
				if (this.tab === "domains") this.loadDomains();
				this.$watch("tab", function (tab) { if (tab === "domains") self.loadDomains(); });
				// reload when a domain / TLS job of this site finishes
				this.$watch("site && site.jobs ? site.jobs.filter(function (j) { return /domain|tls/.test(j.kind); }).map(function (j) { return j.name + j.status; }).join() : ''", function () {
					if (self.tab === "domains") self.loadDomains(true);
				});
			},
			destroy: function () { clearTimeout(this._dnTimer); },

			siteName: function () { return (this.site && this.site.name) || (nx() && nx().query("name")); },

			loadDomains: function (quiet) {
				var self = this;
				var name = this.siteName();
				if (!name) return Promise.resolve();
				if (!quiet) this.dnLoading = true;
				return call("get_domains", { site: name }).then(
					function (r) { self.dn = r; self.dnError = null; self.scheduleDomains(); },
					function (e) { self.dnError = e.message; }
				).finally(function () { self.dnLoading = false; });
			},
			scheduleDomains: function () {
				var self = this;
				clearTimeout(this._dnTimer);
				if (!this.dn) return;
				var waiting = this.dn.domains.some(function (d) {
					return ["Pending DNS", "Verifying", "Pending", "Removing"].indexOf(d.status) !== -1 ||
						(d.tls && d.tls.status === "Pending");
				});
				if (!waiting) return;
				this._dnTimer = setTimeout(function () {
					if (self.tab === "domains") self.loadDomains(true);
					else self.scheduleDomains();
				}, 15000);
			},

			domainClass: function (status) { return "badge " + (DOMAIN_CLASS[status] || ""); },
			tlsClass: function (status) { return "badge " + (TLS_CLASS[status] || ""); },
			needsDns: function (d) { return d.status === "Pending DNS" || (d.status === "Broken" && !d.activated_at); },
			canWrite: function () { return this.dn && this.dn.can_write && this.site && this.site.status === "Active"; },
			copy: function (text) { nx().copy(text); },
			ago: function (value) { return nx().since(value); },
			fmt: function (value) { return value ? nx().fmtDate(value) : "—"; },
			daysLeft: function (value) {
				if (!value) return null;
				var d = new Date(String(value).replace(" ", "T"));
				if (isNaN(d.getTime())) return null;
				return Math.floor((d.getTime() - Date.now()) / 86400000);
			},

			previewRecords: function () {
				var self = this;
				var d = this.newDomain.trim().toLowerCase();
				if (!d) { this.preview = null; return; }
				call("preview_records", { site: this.siteName(), domain: d }).then(
					function (r) { self.preview = r; },
					function () { self.preview = null; }
				);
			},
			addDomain: function () {
				var self = this;
				var d = this.newDomain.trim().toLowerCase();
				if (!d) return;
				this.dnBusy.add = true;
				call("add_domain", { site: this.siteName(), domain: d, skip_dns_check: this.skipNew ? 1 : 0 }).then(
					function (r) {
						self.dn = r.state;
						self.newDomain = "";
						self.preview = null;
						self.skipNew = false;
						if (r.job) toast(t("DNS is correct. Adding {0} to the server…", [r.domain]), "success");
						else toast(t("{0} added. Create the DNS record shown below; we check it automatically.", [r.domain]), "info");
						self.scheduleDomains();
						if (self.load) self.load(true);
					},
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.dnBusy.add = false; });
			},
			verify: function (d) {
				var self = this;
				this.dnBusy[d.domain] = true;
				call("verify_domain", { site: this.siteName(), domain: d.domain }).then(
					function (r) {
						toast(r.message, r.ok ? "success" : "error");
						return self.loadDomains(true).then(function () { if (r.job && self.load) self.load(true); });
					},
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.dnBusy[d.domain] = false; });
			},
			remove: function (d) {
				var self = this;
				if (!window.confirm(t("Remove domain {0}?", [d.domain]))) return;
				this.dnBusy[d.domain] = true;
				call("remove_domain", { site: this.siteName(), domain: d.domain }).then(
					function () { return self.loadDomains(true).then(function () { if (self.load) self.load(true); }); },
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.dnBusy[d.domain] = false; });
			},
			toggleSkip: function (d) {
				var self = this;
				var skip = !d.skip_dns_check;
				if (skip && !window.confirm(t("Skip DNS verification for {0} and add it to the server now?", [d.domain]))) return;
				this.dnBusy[d.domain] = true;
				call("set_skip_dns_check", { site: this.siteName(), domain: d.domain, skip: skip ? 1 : 0 }).then(
					function () { return self.loadDomains(true).then(function () { if (self.load) self.load(true); }); },
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.dnBusy[d.domain] = false; });
			},
			checkTls: function () {
				var self = this;
				this.dnBusy.tls = true;
				call("check_tls", { site: this.siteName() }).then(
					function () {
						toast(t("Checking certificates…"), "info");
						if (self.load) self.load(true);
					},
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.dnBusy.tls = false; });
			},
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxDomains", factory);
	});
})();
