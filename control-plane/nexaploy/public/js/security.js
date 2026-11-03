/* Nexaploy portal: prepaid credits, activity log, account (profile / 2FA / API key) and team
 * security components. Loaded after dashboard.js (uses window.nx and window.__).
 *
 * - nxCredits       /dashboard/billing  nexaploy.api.billing.get_credits / topup
 * - nxActivity      /dashboard/activity nexaploy.api.teams.get_activity
 * - nxAccount       /dashboard/account  nexaploy.api.account.*
 * - nxTeamSecurity  /dashboard/team     nexaploy.api.teams.set_two_factor
 */
(function () {
	"use strict";

	var TYPE_LABELS = {
		TopUp: "Top-up",
		InvoicePayment: "Invoice payment",
		Refund: "Refund",
		Promo: "Promotional credit",
		Adjustment: "Adjustment",
	};

	function nx() { return window.nx; }
	function tr(msg, args) { return window.__ ? window.__(msg, args) : msg; }

	function flatDetails(obj) {
		if (!obj || typeof obj !== "object") return obj === null || obj === undefined ? [] : [["", String(obj)]];
		return nx().flatten(obj).map(function (kv) {
			var v = kv[1];
			if (v === null || v === undefined || v === "") v = "—";
			return [kv[0], String(v)];
		});
	}

	document.addEventListener("alpine:init", function () {
		var Alpine = window.Alpine;

		Alpine.data("nxCredits", function () {
			return {
				loading: true,
				error: null,
				data: null,
				amount: "",
				gateway: "",
				busy: false,
				paidNotice: nx().query("paid") === "1",
				init: function () { this.load(); },
				load: function () {
					var self = this;
					this.loading = true;
					return nx().call("nexaploy.api.billing.get_credits").then(
						function (r) {
							self.data = r;
							self.error = null;
							if (!self.amount) self.amount = String(Math.max(r.min_topup || 0, 10));
							if (!self.gateway && r.gateways && r.gateways.length) self.gateway = r.gateways[0];
						},
						function (e) {
							// billing not installed/enabled yet: the invoices card says so
							if (!/not found|has no attribute|not whitelisted/i.test(e.message || "")) self.error = e.message;
						}
					).finally(function () { self.loading = false; });
				},
				otherBalances: function () {
					var out = {};
					if (!this.data || !this.data.balances) return out;
					var cur = this.data.currency;
					Object.keys(this.data.balances).forEach(function (k) {
						if (k !== cur && Number(this.data.balances[k])) out[k] = this.data.balances[k];
					}, this);
					return out;
				},
				typeLabel: function (t) { return tr(TYPE_LABELS[t] || t); },
				describe: function (t) {
					if (t.description === "signup") return tr("Signup credits");
					return t.description || [t.reference_doctype ? tr(t.reference_doctype) : "", t.reference_name || ""].join(" ").trim();
				},
				signed: function (t) {
					var n = Number(t.amount || 0);
					return (n > 0 ? "+" : "") + nx().money(n, t.currency);
				},
				topup: function () {
					var self = this;
					var amount = Number(this.amount);
					if (!amount || amount <= 0) return nx().toast(tr("Enter a valid amount"), "error");
					if (!this.gateway) return nx().toast(tr("No payment method is available. Please contact support."), "error");
					this.busy = true;
					nx().call("nexaploy.api.billing.topup", { amount: amount, gateway: this.gateway }).then(
						function (r) {
							if (r && r.url) {
								window.location.href = r.url;
								return;
							}
							self.busy = false;
							self.load();
						},
						function (e) { nx().toast(e.message, "error"); self.busy = false; }
					);
				},
			};
		});

		Alpine.data("nxActivity", function () {
			return {
				loading: false,
				error: null,
				rows: [],
				actions: [],
				users: [],
				page: 1,
				hasMore: false,
				filters: { action: "", user: "", date_from: "", date_to: "" },
				init: function () {
					var q = nx().query("action");
					if (q) this.filters.action = q;
					if (nx().can("admin")) this.load();
				},
				load: function () {
					var self = this;
					this.loading = true;
					var args = { page: this.page, page_length: 50 };
					Object.keys(this.filters).forEach(function (k) { if (self.filters[k]) args[k] = self.filters[k]; });
					return nx().call("nexaploy.api.teams.get_activity", args).then(
						function (r) {
							self.rows = r.rows || [];
							self.actions = r.actions || [];
							self.users = r.users || [];
							self.hasMore = !!r.has_more;
							self.error = null;
						},
						function (e) { self.error = e.message; }
					).finally(function () { self.loading = false; });
				},
				groups: function () {
					var seen = {};
					this.actions.forEach(function (a) { seen[a.split(".")[0]] = true; });
					return Object.keys(seen).sort();
				},
				details: function (r) { return flatDetails(r.details); },
				search: function () { this.page = 1; this.load(); },
				reset: function () {
					this.filters = { action: "", user: "", date_from: "", date_to: "" };
					this.search();
				},
				go: function (page) { this.page = Math.max(1, page); this.load(); },
			};
		});

		Alpine.data("nxAccount", function () {
			return {
				loading: true,
				error: null,
				account: null,
				form: { first_name: "", last_name: "", language: "en" },
				secret: null,
				busy: {},
				init: function () { this.load(); },
				load: function () {
					var self = this;
					this.loading = true;
					return nx().call("nexaploy.api.account.get_account").then(
						function (r) {
							self.account = r;
							self.form = { first_name: r.first_name || "", last_name: r.last_name || "", language: r.language || "en" };
							self.error = null;
						},
						function (e) { self.error = e.message; }
					).finally(function () { self.loading = false; });
				},
				saveProfile: function () {
					var self = this;
					var langChanged = this.account && this.form.language !== this.account.language;
					this.busy.profile = true;
					nx().call("nexaploy.api.account.update_profile", this.form).then(
						function () {
							if (langChanged) { window.location.reload(); return; }
							nx().toast(tr("Saved"), "success");
							self.load();
						},
						function (e) { nx().toast(e.message, "error"); }
					).finally(function () { self.busy.profile = false; });
				},
				generate: function (rotate) {
					var self = this;
					if (rotate && !window.confirm(tr("Rotate the API key? Scripts using the current key stop working."))) return;
					this.busy.key = true;
					nx().call("nexaploy.api.account.generate_api_key", { rotate: rotate ? 1 : 0 }).then(
						function (r) {
							self.secret = r;
							self.account.api_key = r.api_key;
							self.account.has_api_secret = true;
						},
						function (e) { nx().toast(e.message, "error"); }
					).finally(function () { self.busy.key = false; });
				},
				revoke: function () {
					var self = this;
					if (!window.confirm(tr("Revoke the API key? Scripts using it stop working."))) return;
					this.busy.revoke = true;
					nx().call("nexaploy.api.account.revoke_api_key").then(
						function () {
							self.secret = null;
							self.account.api_key = null;
							self.account.has_api_secret = false;
							nx().toast(tr("API key revoked"), "success");
						},
						function (e) { nx().toast(e.message, "error"); }
					).finally(function () { self.busy.revoke = false; });
				},
				snippet: function () {
					var a = this.account || {};
					var key = (this.secret && this.secret.api_key) || a.api_key || "<api_key>";
					var secret = (this.secret && this.secret.api_secret) || "<api_secret>";
					return "curl -s " + (a.api_base_url || "") + "/api/method/nexaploy.api.dashboard.list_sites \\\n" +
						"  -H \"Authorization: token " + key + ":" + secret + "\" \\\n" +
						"  -H \"" + (a.team_header || "X-Nexaploy-Team") + ": " + (a.team || "<team>") + "\"";
				},
			};
		});

		Alpine.data("nxTeamSecurity", function () {
			return {
				required: false,
				systemOff: false,
				busy: false,
				init: function () {
					var team = this.team || null;
					this.required = !!(team && team.two_factor_required);
				},
				toggle: function (value) {
					var self = this;
					var msg = value
						? tr("Require two-factor authentication for every member of this team?")
						: tr("Stop requiring two-factor authentication for this team?");
					if (!window.confirm(msg)) return this.reset(value);
					this.busy = true;
					nx().call("nexaploy.api.teams.set_two_factor", { required: value ? 1 : 0 }).then(
						function (r) {
							self.required = !!r.two_factor_required;
							self.systemOff = !r.system_2fa;
							nx().toast(tr("Saved"), "success");
						},
						function (e) { nx().toast(e.message, "error"); self.reset(value); }
					).finally(function () { self.busy = false; });
				},
				reset: function (attempted) {
					// undo the checkbox change the user cancelled / that failed
					var self = this;
					this.required = attempted;
					this.$nextTick(function () { self.required = !attempted; });
				},
			};
		});
	});
})();
