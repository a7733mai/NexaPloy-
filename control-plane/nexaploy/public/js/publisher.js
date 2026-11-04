/* Nexaploy portal: marketplace publisher (profile, listings, revenue, payouts).
 *
 * Served as /assets/nexaploy/js/publisher.js and included as a plain <script> in the head block of
 * www/dashboard/publisher.html so Alpine.data("nxPublisher") registers before Alpine starts.
 * API: nexaploy.api.publisher.* (team-scoped; manage = Owner/Admin, revenue = Owner/Admin/Billing).
 */
(function () {
	"use strict";

	var M = "nexaploy.api.publisher.";
	var STATUS = { Draft: "info", "In Review": "warning", Published: "success", Rejected: "danger", Approved: "info", Paid: "success" };
	var RELEASE_STATUS = { Verified: "success", "Pending verification": "warning pulse", Failed: "danger" };

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { if (nx()) nx().toast(msg, kind); }

	function factory() {
		return {
			loading: true,
			busy: false,
			error: null,
			pub: null,
			perms: { manage: false, revenue: false },
			payoutMethods: ["Credits", "Bank", "PayPal"],
			categories: [],
			listings: [],
			customApps: [],
			revenue: { months: [], payouts: [] },
			editing: false,
			form: {},
			listingForm: null,
			relApp: null,
			relForm: { frappe_major: "16", branch: "", commit: "" },
			relLog: null,
			_relTimer: null,

			init: function () { this.load(); },

			statusClass: function (status) { return "badge " + (STATUS[status] || ""); },
			releaseClass: function (status) { return "badge " + (RELEASE_STATUS[status] || ""); },

			load: function () {
				var self = this;
				this.loading = true;
				return call("get_publisher").then(function (r) {
					self.pub = r.publisher;
					self.perms = r.permissions;
					self.payoutMethods = r.payout_methods;
					self.categories = r.categories;
					self.error = null;
					if (!self.pub) return null;
					return Promise.all([self.loadListings(), self.perms.revenue ? self.loadRevenue() : null]);
				}).catch(function (e) { self.error = e.message; }).finally(function () { self.loading = false; });
			},

			loadListings: function () {
				var self = this;
				return call("list_listings").then(function (r) { self.listings = r.listings; });
			},

			loadRevenue: function () {
				var self = this;
				return call("get_revenue").then(function (r) { self.revenue = r; });
			},

			editProfile: function () {
				var p = this.pub || {};
				this.form = {
					title: p.title || (nx() && nx().boot.team_title) || "",
					website: p.website || "",
					support_email: p.support_email || "",
					payout_method: p.payout_method || "Credits",
					payout_details: p.payout_details || "",
					payout_account: "",
				};
				this.editing = true;
			},

			saveProfile: function () {
				var self = this;
				this.busy = true;
				return call("save_publisher", this.form).then(function (r) {
					self.editing = false;
					toast(t("Publisher profile saved"), "success");
					var first = !self.pub;
					self.pub = r;
					if (first) return self.load();
					return null;
				}).catch(function (e) { toast(e.message, "error"); }).finally(function () { self.busy = false; });
			},

			canEdit: function (field) {
				var f = this.listingForm;
				if (!f) return false;
				if (!f.name) return true;
				return (f.editable || []).indexOf(field) !== -1;
			},

			openNew: function () {
				var self = this;
				this.listingForm = { custom_app: "", title: "", category: "", pricing: "Free", price_monthly: 0, description: "", logo: "", documentation_url: "", screenshots: "", required_apps: "" };
				return call("listable_custom_apps").then(function (r) { self.customApps = r; })
					.catch(function (e) { toast(e.message, "error"); });
			},

			openEdit: function (l) {
				this.listingForm = {
					name: l.name, editable: l.editable, title: l.title, category: l.category || "", pricing: l.pricing,
					price_monthly: l.price_monthly, description: l.description || "", logo: l.logo || "",
					documentation_url: l.documentation_url || "", screenshots: (l.screenshots || []).join("\n"),
					required_apps: (l.required_apps || []).join(", "),
				};
			},

			saveListing: function () {
				var self = this;
				var f = this.listingForm;
				var req;
				if (f.name) {
					var values = {};
					(f.editable || []).forEach(function (k) { if (k in f) values[k] = f[k]; });
					req = call("update_listing", { app: f.name, values: values });
				} else {
					req = call("create_listing", f);
				}
				this.busy = true;
				return req.then(function () {
					self.listingForm = null;
					toast(t("Listing saved"), "success");
					return self.loadListings();
				}).catch(function (e) { toast(e.message, "error"); }).finally(function () { self.busy = false; });
			},

			_act: function (method, l, message) {
				var self = this;
				this.busy = true;
				return call(method, { app: l.name }).then(function () {
					toast(message, "success");
					return self.loadListings();
				}).catch(function (e) { toast(e.message, "error"); }).finally(function () { self.busy = false; });
			},

			submit: function (l) {
				if (!window.confirm(t("Submit {0} for review?", [l.title]))) return null;
				return this._act("submit_listing", l, t("Submitted for review"));
			},

			withdraw: function (l) { return this._act("withdraw_listing", l, t("Listing withdrawn")); },

			/* ---------------------------------------------------------- releases & verification */
			openReleases: function (l) {
				this.relApp = l;
				this.relLog = null;
				var have = l.releases.map(function (r) { return r.frappe_major; });
				var major = ["16", "15", "14"].filter(function (m) { return have.indexOf(m) === -1; })[0] || "16";
				this.relForm = { frappe_major: major, branch: "", commit: "" };
				this._watchPending();
			},
			_setApp: function (l) {
				this.listings = this.listings.map(function (x) { return x.name === l.name ? Object.assign({}, x, l) : x; });
				if (this.relApp && this.relApp.name === l.name) this.relApp = Object.assign({}, this.relApp, l);
				this._watchPending();
			},
			_watchPending: function () {
				// refresh while a test build runs, so Verified / Failed shows up without a reload
				var self = this;
				clearTimeout(this._relTimer);
				if (!this.relApp || !this.relApp.releases.some(function (r) { return r.status === "Pending verification" && r.verification_job; })) return;
				this._relTimer = setTimeout(function () {
					if (!self.relApp) return;
					call("list_listings").then(function (r) {
						self.listings = r.listings;
						var l = r.listings.filter(function (x) { return x.name === self.relApp.name; })[0];
						if (l) self._setApp(l);
						if (self.relLog) self.showLog({ frappe_major: self.relLog.frappe_major });
					}).catch(function () {});
				}, 15000);
			},
			editRelease: function (r) {
				this.relForm = { frappe_major: String(r.frappe_major), branch: r.branch || "", commit: r.commit || "" };
			},
			saveRelease: function () {
				var self = this;
				this.busy = true;
				return call("save_release", { app: this.relApp.name, frappe_major: this.relForm.frappe_major, branch: this.relForm.branch.trim(), commit: this.relForm.commit.trim() || null })
					.then(function (l) {
						self._setApp(l);
						self.relForm.branch = "";
						self.relForm.commit = "";
						toast(t("Release saved. A test build was started."), "success");
					}).catch(function (e) { toast(e.message, "error"); }).finally(function () { self.busy = false; });
			},
			reverify: function (r) {
				var self = this;
				this.busy = true;
				return call("verify_release", { app: this.relApp.name, frappe_major: r.frappe_major })
					.then(function (l) { self._setApp(l); toast(t("Test build started"), "success"); })
					.catch(function (e) { toast(e.message, "error"); }).finally(function () { self.busy = false; });
			},
			removeRelease: function (r) {
				var self = this;
				if (!window.confirm(t("Remove the release for Frappe {0}?", ["v" + r.frappe_major]))) return null;
				this.busy = true;
				return call("remove_release", { app: this.relApp.name, frappe_major: r.frappe_major })
					.then(function (l) { self._setApp(l); })
					.catch(function (e) { toast(e.message, "error"); }).finally(function () { self.busy = false; });
			},
			showLog: function (r) {
				var self = this;
				return nx().call(M + "release_log", { app: this.relApp.name, frappe_major: r.frappe_major })
					.then(function (res) { self.relLog = res; })
					.catch(function (e) { toast(e.message, "error"); });
			},
			formatLog: function (log) {
				if (!log) return "";
				var out = (log.steps || []).map(function (s) {
					return "== [" + s.idx + "] " + (s.name || "") + " (" + s.status + ")" + (s.output ? "\n" + s.output : "");
				});
				if (log.error) out.push("ERROR: " + log.error);
				return out.join("\n");
			},
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxPublisher", factory);
	});
})();
