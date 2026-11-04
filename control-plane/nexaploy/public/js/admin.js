/* Nexaploy portal: platform Administrator area (/dashboard/admin).
 *
 * Served as /assets/nexaploy/js/admin.js and included as a plain <script> in the head block of
 * www/dashboard/admin.html so Alpine.data("nxAdmin") registers before Alpine starts.
 * API: nexaploy.api.admin.* (platform admins only, checked by the server on every call).
 * Secrets are write-only: the server returns {set, hint}; an empty input keeps the stored value,
 * "Clear" sends "__clear__".
 */
(function () {
	"use strict";

	var M = "nexaploy.api.admin.";
	var CLEAR = "__clear__";
	var TABS = ["overview", "features", "storage", "email", "agents", "marketplace", "teams"];
	var LOADERS = {
		overview: ["overview"],
		features: ["get_features"],
		storage: ["get_storage"],
		email: ["get_email_brands"],
		agents: ["get_agents"],
		marketplace: ["get_marketplace"],
		teams: ["list_teams", "list_servers"],
	};
	var TERMINAL = ["Success", "Failure"];

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { if (nx()) nx().toast(msg, kind); }
	function query(name) { return new URLSearchParams(location.search).get(name); }
	function copyObj(o) { return JSON.parse(JSON.stringify(o || {})); }

	function pick(obj, keys) {
		var out = {};
		keys.forEach(function (k) { out[k] = obj ? obj[k] : null; });
		return out;
	}

	function secretsPayload(sec) {
		var out = {};
		Object.keys(sec || {}).forEach(function (k) { if (sec[k]) out[k] = sec[k]; });
		return out;
	}

	function emptyProfile() {
		return { profile_name: "", title: "", provider: "AWS", region_label: "", endpoint: "", region: "",
			addressing: "Preset default", bucket: "", path_prefix: "", access_key: "", secret_key: "", enabled: 1, team: "" };
	}

	function emptyPlan() {
		return { title: "", plan_type: "Site", enabled: 1, price_monthly: 0, currency: "USD", max_sites: 0, plan_order: 0,
			cpu_seconds_per_day: 0, overage_action: "Notify", max_storage_mb: 0, max_database_mb: 0, description: "" };
	}

	function emptyBrand() {
		return { brand_name: "", title: "", enabled: 1, team: "", is_default: 0, primary_color: "#2f5bea", logo: "", favicon: "",
			portal_domains: "", support_email: "", footer_text: "", email_sender_name: "", email_sender_address: "" };
	}

	function emptyRelease() {
		return { version: "", channel: "stable", git_ref: "", repo_url: "", wheel_url: "", wheel_sha256: "", published: 0, release_notes: "" };
	}

	function factory() {
		return {
			tab: "overview",
			loaded: {},
			loading: false,
			busy: false,
			error: null,
			ov: null,
			feat: null,
			stor: null,
			mail: null,
			agents: null,
			market: null,
			teams: [],
			servers: [],
			teamQuery: "",
			// forms
			planForm: null,
			planName: null,
			platformForm: {},
			billingForm: {},
			storageForm: {},
			addonForm: {},
			oauthForm: {},
			relayForm: {},
			sec: { platform: {}, billing: {}, oauth: {}, relay: {} },
			profileForm: null,
			profileEditing: null,
			backupProfile: "",
			tests: {},
			brandForm: null,
			brandName: null,
			releaseForm: null,
			releaseName: null,
			bulk: null,
			notes: {},
			overrides: {},

			init: function () {
				var self = this;
				var tab = query("tab");
				this.show(TABS.indexOf(tab) >= 0 ? tab : "overview", true);
				window.addEventListener("popstate", function () {
					var tb = query("tab");
					self.show(TABS.indexOf(tb) >= 0 ? tb : "overview", true);
				});
			},

			show: function (tab, noPush) {
				this.tab = tab;
				if (!noPush) history.pushState(null, "", "/dashboard/admin" + (tab === "overview" ? "" : "?tab=" + tab));
				if (!this.loaded[tab]) this.load(tab);
			},

			load: function (tab) {
				var self = this;
				tab = tab || this.tab;
				this.loading = true;
				return Promise.all(LOADERS[tab].map(function (m) {
					return call(m, m === "list_teams" ? { query: self.teamQuery || null } : {});
				})).then(function (res) {
					self.apply(tab, res);
					self.loaded[tab] = true;
					self.error = null;
				}).catch(function (e) { self.error = e.message; }).finally(function () { self.loading = false; });
			},

			apply: function (tab, res) {
				var r = res[0];
				if (tab === "overview") this.ov = r;
				if (tab === "features") this.setFeatures(r);
				if (tab === "storage") this.setStorage(r);
				if (tab === "email") this.setMail(r);
				if (tab === "agents") this.agents = r;
				if (tab === "marketplace") this.market = r;
				if (tab === "teams") { this.teams = r.teams; this.servers = res[1].servers; }
			},

			setFeatures: function (r) {
				this.feat = r;
				this.platformForm = copyObj(r.platform);
				this.billingForm = copyObj(r.billing);
				this.setStorageForms(r.storage);
				this.sec.platform = {};
				this.sec.billing = {};
			},

			setStorageForms: function (view) {
				this.storageForm = copyObj(view.settings);
				var addons = {};
				Object.keys(view.addons || {}).forEach(function (k) {
					var p = view.addons[k];
					addons[k] = p ? { price_monthly: p.price_monthly, currency: p.currency, enabled: p.enabled ? 1 : 0 } : null;
				});
				this.addonForm = addons;
				if (this.feat) this.feat.storage = view;
			},

			setStorage: function (r) {
				this.stor = r;
				this.oauthForm = copyObj(r.oauth);
				this.sec.oauth = {};
				this.backupProfile = (r.settings && r.settings.backup_storage_profile) || "";
			},

			setMail: function (r) {
				this.mail = r;
				this.relayForm = copyObj(r.relay);
				this.sec.relay = {};
			},

			run: function (method, args, message) {
				var self = this;
				this.busy = true;
				return call(method, args).then(function (r) {
					if (message) toast(message, "success");
					return r;
				}).catch(function (e) { toast(e.message, "error"); throw e; }).finally(function () { self.busy = false; });
			},

			secretLabel: function (state) {
				if (!state || !state.set) return t("Not set");
				return state.hint ? t("Set") + " (" + state.hint + ")" : t("Set");
			},

			clearSecret: function (group, field) {
				if (!window.confirm(t("Remove the stored value of {0}?", [field]))) return;
				this.sec[group][field] = CLEAR;
			},

			money: function (amounts) {
				var keys = Object.keys(amounts || {});
				if (!keys.length) return nx().money(0, (this.ov && this.ov.currency) || "USD");
				return keys.map(function (c) { return nx().money(amounts[c], c); }).join(" · ");
			},

			entries: function (obj) {
				return Object.keys(obj || {}).map(function (k) { return { key: k, value: obj[k] }; });
			},

			// ---------------------------------------------------------- features & pricing
			newPlan: function () { this.planName = null; this.planForm = emptyPlan(); },
			editPlan: function (p) { this.planName = p.name; this.planForm = copyObj(p); },
			savePlan: function () {
				var self = this;
				var values = copyObj(this.planForm);
				delete values.name;
				if (this.planName) delete values.title;
				return this.run("save_plan", { values: values, name: this.planName }, t("Plan saved")).then(function (plans) {
					self.feat.plans = plans;
					self.planForm = null;
				}, function () {});
			},
			togglePlan: function (p) {
				var self = this;
				return this.run("save_plan", { name: p.name, values: { enabled: p.enabled ? 0 : 1 } }, t("Plan saved")).then(function (plans) {
					self.feat.plans = plans;
				}, function () {});
			},
			savePlatform: function () {
				var self = this;
				return this.run("update_platform_settings", { values: this.platformForm, secrets: secretsPayload(this.sec.platform) },
					t("Settings saved")).then(function (r) {
					self.feat.platform = r.platform;
					self.feat.platform_secrets = r.platform_secrets;
					self.platformForm = copyObj(r.platform);
					self.sec.platform = {};
				}, function () {});
			},
			saveBilling: function () {
				var self = this;
				return this.run("update_billing_settings", { values: this.billingForm, secrets: secretsPayload(this.sec.billing) },
					t("Settings saved")).then(function (r) {
					self.feat.billing = r.billing;
					self.feat.billing_secrets = r.billing_secrets;
					self.billingForm = copyObj(r.billing);
					self.sec.billing = {};
				}, function () {});
			},
			saveStorageSettings: function () {
				var self = this;
				var prices = {};
				var values = copyObj(this.storageForm);
				// numbers left empty keep their stored value; profile/plan links may be cleared with ""
				Object.keys(values).forEach(function (k) {
					if (values[k] === null || (values[k] === "" && !/_profile$|_plan$|^cache_max_size$/.test(k))) delete values[k];
				});
				Object.keys(this.addonForm).forEach(function (k) { if (self.addonForm[k]) prices[k] = self.addonForm[k]; });
				return this.run("update_storage_settings", { values: values, addon_prices: prices },
					t("Settings saved")).then(function (view) {
					self.setStorageForms(view);
					self.loaded.storage = false;
				}, function () {});
			},
			platformProfiles: function (view) {
				return ((view || {}).profiles || []).filter(function (p) { return !p.team; });
			},

			// ---------------------------------------------------------- storage & backups
			newProfile: function () { this.profileEditing = null; this.profileForm = emptyProfile(); },
			editProfile: function (p) {
				this.profileEditing = p.name;
				this.profileForm = Object.assign(emptyProfile(), pick(p, ["title", "provider", "region_label", "endpoint", "region",
					"addressing", "bucket", "path_prefix", "access_key", "team"]), { profile_name: p.name, enabled: p.enabled ? 1 : 0, secret_key: "" });
			},
			saveProfile: function () {
				var self = this;
				var values = copyObj(this.profileForm);
				var team = values.team || null;
				delete values.team;
				if (!values.secret_key) delete values.secret_key;
				return this.run("save_storage_profile", { values: values, team: team }, t("Storage profile saved")).then(function () {
					self.profileForm = null;
					self.loaded.features = false;
					return self.load("storage");
				}, function () {});
			},
			deleteProfile: function (p) {
				var self = this;
				if (!window.confirm(t("Delete storage profile {0}?", [p.name]))) return null;
				return this.run("delete_storage_profile", { name: p.name }, t("Storage profile deleted")).then(function () {
					self.loaded.features = false;
					return self.load("storage");
				}, function () {});
			},
			testProfile: function (p) {
				var self = this;
				this.tests[p.name] = { status: "Pending" };
				return call("test_storage_profile", { name: p.name }).then(function (r) {
					self.tests[p.name] = { status: "Pending", server: r.server, job: r.job };
					self.pollTest(p.name, r.job);
				}).catch(function (e) {
					self.tests[p.name] = { status: "Failure", error: e.message };
				});
			},
			pollTest: function (profile, job) {
				var self = this;
				call("get_job", { name: job }).then(function (j) {
					self.tests[profile] = Object.assign({}, self.tests[profile], { status: j.status, error: j.error, result: j.result });
					if (TERMINAL.indexOf(j.status) < 0) setTimeout(function () { self.pollTest(profile, job); }, 3000);
				}).catch(function (e) { self.tests[profile] = { status: "Failure", error: e.message }; });
			},
			useForBackups: function () {
				var self = this;
				if (!window.confirm(t("Every server will upload its backups to the selected bucket. Continue?"))) return null;
				return this.run("use_profile_for_backups", { profile: this.backupProfile || null }, t("Backup storage updated")).then(function () {
					return self.load("storage");
				}, function () {});
			},
			saveOauth: function () {
				var self = this;
				return this.run("update_oauth_apps", { values: this.oauthForm, secrets: secretsPayload(this.sec.oauth) },
					t("Settings saved")).then(function (r) {
					self.stor.oauth = r.oauth;
					self.stor.oauth_secrets = r.oauth_secrets;
					self.oauthForm = copyObj(r.oauth);
					self.sec.oauth = {};
				}, function () {});
			},

			// ---------------------------------------------------------- e-mail & brands
			saveRelay: function () {
				var self = this;
				return this.run("update_email_relay", { values: this.relayForm, secrets: secretsPayload(this.sec.relay) },
					t("Settings saved")).then(function (r) {
					self.mail.relay = r.relay;
					self.mail.relay_secrets = r.relay_secrets;
					self.relayForm = copyObj(r.relay);
					self.sec.relay = {};
				}, function () {});
			},
			newBrand: function () { this.brandName = null; this.brandForm = emptyBrand(); },
			editBrand: function (b) {
				this.brandName = b.name;
				this.brandForm = Object.assign(emptyBrand(), copyObj(b), { brand_name: b.name });
				delete this.brandForm.name;
				this.brandForm.enabled = b.enabled ? 1 : 0;
				this.brandForm.is_default = b.is_default ? 1 : 0;
				this.brandForm.team = b.team || "";
			},
			saveBrand: function () {
				var self = this;
				var values = copyObj(this.brandForm);
				if (this.brandName) delete values.brand_name;
				return this.run("save_brand", { values: values, name: this.brandName }, t("Brand saved")).then(function (brands) {
					self.mail.brands = brands;
					self.brandForm = null;
				}, function () {});
			},
			deleteBrand: function (b) {
				var self = this;
				if (!window.confirm(t("Delete brand {0}?", [b.title || b.name]))) return null;
				return this.run("delete_brand", { name: b.name }, t("Brand deleted")).then(function (brands) {
					self.mail.brands = brands;
				}, function () {});
			},

			// ---------------------------------------------------------- agent releases
			newRelease: function () { this.releaseName = null; this.releaseForm = emptyRelease(); },
			editRelease: function (r) {
				this.releaseName = r.name;
				this.releaseForm = Object.assign(emptyRelease(), pick(r, ["version", "channel", "git_ref", "repo_url", "wheel_url",
					"release_notes"]), { published: r.published ? 1 : 0 });
			},
			saveRelease: function () {
				var self = this;
				var values = copyObj(this.releaseForm);
				Object.keys(values).forEach(function (k) { if (values[k] === "" || values[k] === null) delete values[k]; });
				if (this.releaseName) delete values.version;
				return this.run("save_agent_release", { values: values, name: this.releaseName }, t("Release saved")).then(function () {
					self.releaseForm = null;
					return self.load("agents");
				}, function () {});
			},
			publish: function (r, value) {
				var self = this;
				return this.run("set_release_published", { name: r.name, published: value ? 1 : 0 },
					value ? t("Release published") : t("Release unpublished")).then(function () { return self.load("agents"); }, function () {});
			},
			bulkUpgrade: function (dry) {
				var self = this;
				if (!dry && !window.confirm(t("Upgrade the agent on every outdated server now?"))) return null;
				return this.run("upgrade_outdated_agents", { dry_run: dry ? 1 : 0 },
					dry ? null : t("Agent upgrades started")).then(function (r) {
					self.bulk = { dry: dry, servers: r.servers };
					if (!dry) return self.load("agents");
				}, function () {});
			},
			upgradeServer: function (s) {
				var self = this;
				if (!window.confirm(t("Upgrade the agent on {0} to {1}?", [s.hostname || s.name, s.latest]))) return null;
				return this.run("upgrade_server_agent", { server: s.name }, t("Agent upgrade started")).then(function () {
					return self.load("agents");
				}, function () {});
			},

			// ---------------------------------------------------------- marketplace
			review: function (app, action) {
				var self = this;
				if (action !== "approve" && !window.confirm(t("Are you sure?"))) return null;
				return this.run("review_listing", { app: app.name, action: action, notes: this.notes[app.name] || null },
					t("Listing updated")).then(function () { return self.load("marketplace"); }, function () {});
			},
			overrideRelease: function (r) {
				var self = this;
				var status = this.overrides[r.app + ":" + r.frappe_major];
				if (!status) return null;
				return this.run("set_release_status", { app: r.app, frappe_major: r.frappe_major, status: status },
					t("Release updated")).then(function () { return self.load("marketplace"); }, function () {});
			},
			reverify: function (r) {
				var self = this;
				return this.run("verify_release", { app: r.app, frappe_major: r.frappe_major }, t("Test build started")).then(function () {
					return self.load("marketplace");
				}, function () {});
			},
			toggleReview: function (rv) {
				var self = this;
				var reason = null;
				if (!rv.hidden) {
					reason = window.prompt(t("Why is this review hidden?"));
					if (reason === null) return null;
				}
				return this.run("hide_review", { review: rv.name, hidden: rv.hidden ? 0 : 1, reason: reason },
					t("Review updated")).then(function (r) { rv.hidden = r.hidden ? 1 : 0; rv.hidden_reason = reason; }, function () {});
			},

			// ---------------------------------------------------------- teams & servers
			searchTeams: function () {
				var self = this;
				return call("list_teams", { query: this.teamQuery || null }).then(function (r) { self.teams = r.teams; })
					.catch(function (e) { toast(e.message, "error"); });
			},
			toggleFlag: function (team, flag) {
				var values = {};
				values[flag] = team[flag] ? 0 : 1;
				return this.run("set_team_flags", { team: team.name, values: values }, t("Team updated")).then(function (r) {
					Object.assign(team, r);
				}, function () {});
			},
			suspend: function (team, on) {
				var msg = on ? t("Suspend every active site of {0}?", [team.title]) : t("Lift the billing suspension of {0}?", [team.title]);
				if (!window.confirm(msg)) return null;
				return this.run(on ? "suspend_team" : "unsuspend_team", { team: team.name },
					on ? t("Team suspended") : t("Team unsuspended")).then(function (r) {
					team.suspended_sites = r.suspended_sites;
				}, function () {});
			},
			grant: function (team) {
				var amount = window.prompt(t("Credits to add to {0} ({1}); negative = adjustment", [team.title, team.currency || ""]));
				if (amount === null || amount === "") return null;
				var description = window.prompt(t("Reason (shown in the team's credit ledger)")) || null;
				var type = Number(amount) < 0 ? "Adjustment" : "Promo";
				return this.run("grant_credits", { team: team.name, amount: amount, type: type, description: description },
					t("Credits updated")).then(function (r) { team.credit_balance = r.balance; }, function () {});
			},
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxAdmin", factory);
	});
})();
