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

			init: function () { this.load(); },

			statusClass: function (status) { return "badge " + (STATUS[status] || ""); },

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
				this.listingForm = { custom_app: "", title: "", category: "", pricing: "Free", price_monthly: 0, description: "", logo: "", documentation_url: "", screenshots: "" };
				return call("listable_custom_apps").then(function (r) { self.customApps = r; })
					.catch(function (e) { toast(e.message, "error"); });
			},

			openEdit: function (l) {
				this.listingForm = {
					name: l.name, editable: l.editable, title: l.title, category: l.category || "", pricing: l.pricing,
					price_monthly: l.price_monthly, description: l.description || "", logo: l.logo || "",
					documentation_url: l.documentation_url || "", screenshots: (l.screenshots || []).join("\n"),
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
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxPublisher", factory);
	});
})();
