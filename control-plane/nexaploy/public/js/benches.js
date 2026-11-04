/* Nexaploy portal: bench groups (customer Release Groups) and custom apps from GitHub.
 *
 * Served as /assets/nexaploy/js/benches.js. Registers the Alpine components `nxBenches`
 * (/dashboard/benches) and `nxBench` (/dashboard/bench?name=). Uses the helpers of
 * dashboard.js (window.nx, window.__) at call time, so it may load before or after it, but
 * it must run before Alpine starts (synchronous <script>, or `defer` placed before Alpine).
 * APIs: nexaploy.api.benches.*, nexaploy.api.github.*
 */
(function () {
	"use strict";
	if (window.__nxBenchesLoaded) return;
	window.__nxBenchesLoaded = true;

	var BENCHES = "nexaploy.api.benches.";
	var GITHUB = "nexaploy.api.github.";

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(method, args); }
	function toast(msg, kind) { return nx().toast(msg, kind); }
	function query(name) { return new URLSearchParams(window.location.search).get(name); }

	var GITHUB_NOTICES = {
		connected: ["success", "GitHub account connected. You can now add apps from its repositories."],
		requested: ["info", "Your request to install the GitHub app was sent to the organization owners."],
		invalid_state: ["danger", "The GitHub connection link expired or was started by another user. Try again."],
		missing_installation: ["danger", "GitHub did not return an installation. Try again."],
		unverified: ["danger", "Could not verify that you can access this GitHub installation."],
		claimed: ["danger", "This GitHub account is already connected to another team."],
	};

	function register() {
		var Alpine = window.Alpine;

		Alpine.data("nxBenches", function () {
			return {
				loading: true,
				error: null,
				groups: [],
				notice: null,
				showNew: false,
				opts: null,
				optsLoading: false,
				majors: ["14", "15", "16"],
				form: { title: "", frappe_major: "16", build_server: "", picks: [] },
				creating: false,
				github: null,
				customApps: [],
				connecting: false,
				gh: { installation: "", repo: "", branch: "", app_name: "" },
				repos: [],
				branches: [],
				reposLoading: false,
				branchesLoading: false,
				adding: false,
				other: { repo_url: "", branch: "", app_name: "", frappe_majors: "", access_token: "", access_username: "oauth2" },
				init: function () {
					var self = this;
					var gh = query("github");
					if (gh && GITHUB_NOTICES[gh]) this.notice = { kind: GITHUB_NOTICES[gh][0], text: t(GITHUB_NOTICES[gh][1]) };
					this.load();
					this.loadGithub();
					if (query("new")) this.openNew();
					this.$watch("form.frappe_major", function () { self.loadOptions(); });
				},
				load: function () {
					var self = this;
					return call(BENCHES + "list_release_groups").then(
						function (r) { self.groups = r; self.error = null; },
						function (e) { self.error = e.message; }
					).finally(function () { self.loading = false; });
				},
				loadGithub: function () {
					var self = this;
					call(GITHUB + "list_installations").then(function (r) {
						self.github = r;
						var active = r.installations.filter(function (i) { return i.status === "Active"; });
						if (active.length === 1 && !self.gh.installation) {
							self.gh.installation = active[0].name;
							self.loadRepos();
						}
					}, function (e) { self.github = { configured: false, installations: [] }; toast(e.message, "error"); });
					call(GITHUB + "list_custom_apps").then(function (r) { self.customApps = r; }, function () {});
				},
				openNew: function () {
					this.showNew = true;
					if (!this.opts) this.loadOptions();
				},
				loadOptions: function () {
					var self = this;
					this.optsLoading = true;
					return call(BENCHES + "options", { frappe_major: this.form.frappe_major }).then(
						function (r) {
							self.opts = r;
							self.majors = r.majors;
							if (!r.has_default_build_server && !self.form.build_server && r.servers.length) {
								self.form.build_server = r.servers[0].name;
							}
							var market = r.marketplace_apps.map(function (a) { return a.app_name; });
							var custom = r.custom_apps.filter(function (a) { return a.compatible; }).map(function (a) { return a.name; });
							self.form.picks = self.form.picks.filter(function (p) {
								return (p.source_type === "Marketplace" ? market : custom).indexOf(p.app) !== -1;
							});
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.optsLoading = false; });
				},
				isPicked: function (source, app) {
					return this.form.picks.some(function (p) { return p.source_type === source && p.app === app; });
				},
				pick: function (source, app) {
					if (this.isPicked(source, app)) {
						this.form.picks = this.form.picks.filter(function (p) { return !(p.source_type === source && p.app === app); });
					} else {
						this.form.picks.push({ source_type: source, app: app });
					}
				},
				create: function () {
					var self = this;
					this.creating = true;
					call(BENCHES + "create_release_group", {
						title: this.form.title.trim().toLowerCase(),
						frappe_major: this.form.frappe_major,
						build_server: this.form.build_server || null,
						apps: this.form.picks,
					}).then(
						function (g) {
							toast(t("Bench group {0} created", [g.title]), "success");
							location.href = "/dashboard/bench?name=" + encodeURIComponent(g.name);
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.creating = false; });
				},
				connectGithub: function () {
					var self = this;
					this.connecting = true;
					call(GITHUB + "install_url").then(
						function (r) { location.href = r.url; },
						function (e) { self.connecting = false; toast(e.message, "error"); }
					);
				},
				loadRepos: function () {
					var self = this;
					this.repos = [];
					this.branches = [];
					this.gh.repo = "";
					this.gh.branch = "";
					if (!this.gh.installation) return;
					this.reposLoading = true;
					call(GITHUB + "list_repos", { installation: this.gh.installation }).then(
						function (r) { self.repos = r; },
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.reposLoading = false; });
				},
				loadBranches: function () {
					var self = this;
					this.branches = [];
					this.gh.branch = "";
					if (!this.gh.repo) return;
					var repo = this.repos.filter(function (r) { return r.full_name === self.gh.repo; })[0];
					this.branchesLoading = true;
					call(GITHUB + "list_branches", { installation: this.gh.installation, repo: this.gh.repo }).then(
						function (r) {
							self.branches = r;
							if (repo && repo.default_branch) self.gh.branch = repo.default_branch;
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.branchesLoading = false; });
				},
				afterAdd: function (app) {
					toast(t("Custom app {0} added", [app.app_name]), "success");
					this.loadGithub();
					if (this.opts) this.loadOptions();
				},
				addFromGithub: function () {
					var self = this;
					this.adding = true;
					call(GITHUB + "add_custom_app", {
						installation: this.gh.installation,
						repo: this.gh.repo,
						branch: this.gh.branch || null,
						app_name: this.gh.app_name.trim() || null,
					}).then(
						function (app) { self.gh.app_name = ""; self.afterAdd(app); },
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.adding = false; });
				},
				addFromUrl: function () {
					var self = this;
					this.adding = true;
					var args = Object.assign({}, this.other);
					call(GITHUB + "add_custom_app_from_url", args).then(
						function (app) {
							self.other = { repo_url: "", branch: "", app_name: "", frappe_majors: "", access_token: "", access_username: "oauth2" };
							self.afterAdd(app);
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.adding = false; });
				},
				toggleApp: function (app) {
					var self = this;
					var status = app.status === "Active" ? "Disabled" : "Active";
					call(GITHUB + "set_custom_app_status", { name: app.name, status: status }).then(
						function () { self.loadGithub(); },
						function (e) { toast(e.message, "error"); }
					);
				},
			};
		});

		Alpine.data("nxBench", function () {
			return {
				name: query("name"),
				loading: true,
				error: null,
				group: null,
				timer: null,
				deploying: false,
				busy: false,
				target: "",
				addingApp: false,
				newApp: { source_type: "Marketplace", app: "", branch: "" },
				opts: null,
				openDeploy: null,
				deployDetail: null,
				openSteps: {},
				_deployTimer: null,
				init: function () {
					var self = this;
					if (!this.name) {
						this.loading = false;
						this.error = t("No bench group selected");
						return;
					}
					this.load().then(function () {
						var d = self.group && self.group.deploys[0];
						if (d && (d.status === "Pending" || d.status === "Building")) self.toggleDeploy(d.name);
					});
					this.timer = setInterval(function () { self.load(true); }, 10000);
					this.$watch("newApp.source_type", function () { self.newApp.app = ""; });
				},
				destroy: function () { clearInterval(this.timer); clearTimeout(this._deployTimer); },
				load: function (quiet) {
					var self = this;
					return call(BENCHES + "get_release_group", { name: this.name }).then(
						function (r) { self.group = r; self.error = null; },
						function (e) { if (!quiet || !self.group) self.error = e.message; }
					).finally(function () { self.loading = false; });
				},
				get building() {
					return !!(this.group && this.group.deploys.some(function (d) { return d.status === "Pending" || d.status === "Building"; }));
				},
				serverLabel: function (name) {
					if (!name || !this.group) return "";
					var s = this.group.servers.filter(function (x) { return x.name === name; })[0];
					return s ? (s.hostname || s.name) : name;
				},
				deploy: function () {
					var self = this;
					this.deploying = true;
					call(BENCHES + "deploy", { release_group: this.name }).then(
						function (r) {
							toast(t("Build started"), "success");
							return self.load().then(function () { self.toggleDeploy(r.deploy_candidate, true); });
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.deploying = false; });
				},
				openAddApp: function () {
					var self = this;
					this.addingApp = true;
					if (!this.opts) {
						call(BENCHES + "options", { frappe_major: this.group.frappe_major }).then(
							function (r) { self.opts = r; },
							function (e) { toast(e.message, "error"); }
						);
					}
				},
				get appChoices() {
					if (!this.opts || !this.group) return [];
					var used = this.group.apps.map(function (a) { return a.app_name; });
					if (this.newApp.source_type === "Custom") {
						return this.opts.custom_apps.map(function (a) {
							return {
								value: a.name,
								label: a.title + " (" + a.app_name + ")" + (a.compatible ? "" : " · " + t("not compatible")),
								disabled: !a.compatible || used.indexOf(a.app_name) !== -1,
							};
						});
					}
					return this.opts.marketplace_apps.map(function (a) {
						return { value: a.app_name, label: a.title + " (" + a.app_name + ")", disabled: used.indexOf(a.app_name) !== -1 };
					});
				},
				addApp: function () {
					var self = this;
					this.busy = true;
					call(BENCHES + "add_app", {
						release_group: this.name,
						source_type: this.newApp.source_type,
						app: this.newApp.app,
						branch: this.newApp.branch.trim() || null,
					}).then(
						function (g) {
							self.group = g;
							self.addingApp = false;
							self.newApp = { source_type: self.newApp.source_type, app: "", branch: "" };
							toast(t("App added. Deploy to include it in a new image."), "success");
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.busy = false; });
				},
				removeApp: function (app) {
					var self = this;
					if (!window.confirm(t("Remove {0} from this bench group?", [app.app_name]))) return;
					this.busy = true;
					call(BENCHES + "remove_app", { release_group: this.name, app: app.app_name }).then(
						function (g) { self.group = g; toast(t("App removed"), "success"); },
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.busy = false; });
				},
				deployToServer: function () {
					var self = this;
					this.busy = true;
					call(BENCHES + "deploy_to_server", { release_group: this.name, server: this.target }).then(
						function (r) {
							toast(r.action === "update" ? t("Updating bench {0}", [r.bench]) : t("Creating bench {0}", [r.bench]), "success");
							self.target = "";
							self.load(true);
						},
						function (e) { toast(e.message, "error"); }
					).finally(function () { self.busy = false; });
				},
				toggleDeploy: function (name, forceOpen) {
					clearTimeout(this._deployTimer);
					if (this.openDeploy === name && !forceOpen) {
						this.openDeploy = null;
						this.deployDetail = null;
						return;
					}
					this.openDeploy = name;
					this.deployDetail = null;
					this.openSteps = {};
					this.pollDeploy(name);
				},
				pollDeploy: function (name) {
					var self = this;
					call(BENCHES + "get_deploy", { name: name }).then(
						function (d) {
							if (self.openDeploy !== name) return;
							self.deployDetail = d;
							(d.steps || []).forEach(function (s) {
								if (s.status === "running" || s.status === "failure") self.openSteps[s.idx] = true;
							});
							if (d.active) {
								self._deployTimer = setTimeout(function () { self.pollDeploy(name); }, 3000);
							} else {
								self.load(true);
							}
						},
						function (e) { toast(e.message, "error"); }
					);
				},
			};
		});
	}

	if (window.Alpine && window.Alpine.version) {
		// Alpine already started (script loaded late): register and initialise our trees.
		register();
		document.querySelectorAll("[x-data='nxBenches'], [x-data='nxBench']").forEach(function (el) {
			if (!el._x_dataStack) window.Alpine.initTree(el);
		});
	} else {
		document.addEventListener("alpine:init", register);
	}
})();
