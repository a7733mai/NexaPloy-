/* Nexaploy portal: site tools (usage & plan upgrade, backup downloads, site config editor).
 *
 * Served as /assets/nexaploy/js/site_tools.js. Must run BEFORE Alpine starts: include it as a
 * plain (non-deferred) <script> - www/dashboard/site.html does so in its `head` block - or add
 * `<script defer src="/assets/nexaploy/js/site_tools.js"></script>` to dashboard_base.html
 * before the Alpine tag. Uses window.nx (dashboard.js) lazily, at call time.
 *
 * Alpine.data("nxSiteTools") is nested inside nxSite on /dashboard/site, so `site` and `tab`
 * from the parent scope are visible in templates.
 */
(function () {
	"use strict";

	var M = "nexaploy.api.site_tools.";
	var MASK = "********";

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { return nx().toast(msg, kind); }

	function fmtNum(n, digits) {
		if (n === null || n === undefined || n === "") return "—";
		var v = Number(n);
		if (!isFinite(v)) return "—";
		return v.toLocaleString(undefined, { maximumFractionDigits: digits === undefined ? 1 : digits });
	}

	function display(value) {
		if (typeof value === "string") return value;
		return JSON.stringify(value);
	}

	/* text from the editor -> JSON value. Strings stay strings when the original was a string. */
	function parseValue(text, original) {
		if (typeof original === "string") return text;
		var s = String(text).trim();
		try {
			return JSON.parse(s);
		} catch (e) {
			if (original === undefined) return text; // new key: free text is a string
			throw new Error(t("Invalid JSON value: {0}", [s]));
		}
	}

	function factory() {
		return {
			stName: null,
			usage: null,
			usageError: null,
			usageLoading: false,
			planBusy: false,
			files: {},
			downloading: null,
			cfg: { loading: false, error: null, loaded: false, rows: [], redacted: [], readonly: [], job: null },
			newKey: "",
			newValue: "",
			saving: false,
			_cfgTimer: null,

			init: function () {
				var self = this;
				this.stName = nx() ? nx().query("name") : new URLSearchParams(location.search).get("name");
				if (!this.stName) return;
				this.loadUsage();
				this.loadFiles();
				this.$watch("site && site.backups ? site.backups.map(function (b) { return b.name + b.status; }).join() : ''", function () {
					self.loadFiles();
				});
			},
			destroy: function () { clearTimeout(this._cfgTimer); },

			/* ---------------------------------------------------------------- usage */
			loadUsage: function () {
				var self = this;
				this.usageLoading = true;
				return call("get_usage", { site: this.stName }).then(
					function (r) { self.usage = r; self.usageError = null; },
					function (e) { self.usageError = e.message; }
				).finally(function () { self.usageLoading = false; });
			},
			meters: function () {
				var u = this.usage;
				if (!u) return [];
				var m = u.status.metrics;
				return [
					{ key: "cpu", label: t("CPU time today"), unit: t("s"), used: m.cpu.used, limit: m.cpu.limit, percent: m.cpu.percent, status: m.cpu.status },
					{ key: "database", label: t("Database"), unit: "MB", used: m.database.used, limit: m.database.limit, percent: m.database.percent, status: m.database.status },
					{ key: "storage", label: t("File storage"), unit: "MB", used: m.storage.used, limit: m.storage.limit, percent: m.storage.percent, status: m.storage.status },
				];
			},
			meterWidth: function (m) {
				if (!m.limit) return "0%";
				return Math.min(100, Math.max(1, m.percent || 0)) + "%";
			},
			meterText: function (m) {
				if (!m.limit) return fmtNum(m.used) + " " + m.unit + " · " + t("Unlimited");
				return fmtNum(m.used) + " / " + fmtNum(m.limit, 0) + " " + m.unit + " (" + fmtNum(m.percent) + "%)";
			},
			historyMax: function () {
				var h = (this.usage && this.usage.history) || [];
				return h.reduce(function (a, r) { return Math.max(a, r.cpu_seconds || 0); }, 0) || 1;
			},
			barHeight: function (r) {
				return Math.max(2, Math.round(100 * (r.cpu_seconds || 0) / this.historyMax())) + "%";
			},
			fmtNum: fmtNum,
			planLabel: function (p) {
				var parts = [p.title];
				if (p.price_monthly !== undefined && p.price_monthly !== null) parts.push(nx().money(p.price_monthly, p.currency) + " " + t("/ month"));
				return parts.join(" · ");
			},
			planLimits: function (p) {
				var l = [];
				l.push(p.cpu_seconds_per_day ? t("{0} CPU s/day", [fmtNum(p.cpu_seconds_per_day, 0)]) : t("Unlimited CPU"));
				l.push(p.max_database_mb ? t("{0} MB database", [fmtNum(p.max_database_mb, 0)]) : t("Unlimited database"));
				l.push(p.max_storage_mb ? t("{0} MB storage", [fmtNum(p.max_storage_mb, 0)]) : t("Unlimited storage"));
				return l.join(" · ");
			},
			isUpgrade: function (p) { return !!(this.usage && this.usage.upgrades.indexOf(p.name) !== -1); },
			changePlan: function (p) {
				var self = this;
				if (!window.confirm(t("Change the plan of {0} to {1}?", [this.stName, p.title]))) return;
				this.planBusy = true;
				call("change_plan", { site: this.stName, plan: p.name }).then(
					function () {
						toast(t("Plan changed to {0}", [p.title]), "success");
						self.loadUsage();
						if (typeof self.load === "function") self.load(true);
					},
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.planBusy = false; });
			},

			/* ---------------------------------------------------------------- backups */
			loadFiles: function () {
				var self = this;
				return call("list_backup_files", { site: this.stName }).then(
					function (r) { self.files = r || {}; },
					function () { self.files = {}; }
				);
			},
			backupFiles: function (backup) { return this.files[backup] || []; },
			fileLabel: function (ft) {
				return { database: t("Database"), public: t("Public files"), private: t("Private files"), site_config: t("Config") }[ft] || ft;
			},
			download: function (backup, ft) {
				var self = this;
				this.downloading = backup + ":" + ft;
				call("download_backup", { backup: backup, file_type: ft }).then(
					function (r) {
						if (r && r.url) window.location.href = r.url;
					},
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.downloading = null; });
			},

			/* ---------------------------------------------------------------- site config */
			loadConfig: function (refresh) {
				var self = this;
				clearTimeout(this._cfgTimer);
				this.cfg.loading = true;
				this.cfg.error = null;
				call("get_site_config", { site: this.stName, refresh: refresh ? 1 : 0 }).then(
					function (r) { self._onConfig(r, 0); },
					function (e) { self.cfg.loading = false; self.cfg.error = e.message; }
				);
			},
			_onConfig: function (r, attempt) {
				var self = this;
				this.cfg.job = r.job;
				if (r.status === "Success" && r.config) {
					this.cfg.loading = false;
					this.cfg.loaded = true;
					this.cfg.redacted = r.redacted || [];
					this.cfg.readonly = r.readonly || [];
					this.cfg.rows = Object.keys(r.config).sort().map(function (k) {
						var v = r.config[k];
						return {
							key: k, original: v, text: display(v), origText: display(v),
							secret: self.cfg.redacted.indexOf(k) !== -1,
							readonly: self.cfg.readonly.indexOf(k) !== -1,
							removed: false, isNew: false,
						};
					});
					return;
				}
				if (r.status === "Failure") {
					this.cfg.loading = false;
					this.cfg.error = r.error || t("Could not read the site config");
					return;
				}
				if (attempt > 90) {
					this.cfg.loading = false;
					this.cfg.error = t("The server agent did not answer. Try again later.");
					return;
				}
				this._cfgTimer = setTimeout(function () {
					call("get_site_config_result", { site: self.stName, job: r.job }).then(
						function (x) { self._onConfig(x, attempt + 1); },
						function (e) { self.cfg.loading = false; self.cfg.error = e.message; }
					);
				}, 2000);
			},
			addRow: function () {
				var key = this.newKey.trim();
				if (!/^[A-Za-z_][A-Za-z0-9_]{0,99}$/.test(key)) return toast(t("Invalid config key"), "error");
				if (this.cfg.rows.some(function (r) { return r.key === key; })) return toast(t("{0} already exists", [key]), "error");
				this.cfg.rows.push({ key: key, original: undefined, text: this.newValue, origText: null, secret: false, readonly: false, removed: false, isNew: true });
				this.newKey = "";
				this.newValue = "";
			},
			dirty: function () {
				return this.cfg.rows.some(function (r) { return r.removed || r.isNew || r.text !== r.origText; });
			},
			saveConfig: function () {
				var self = this;
				var changes = {};
				var remove = [];
				try {
					this.cfg.rows.forEach(function (r) {
						if (r.readonly) return;
						if (r.removed) {
							if (!r.isNew) remove.push(r.key);
							return;
						}
						if (!r.isNew && r.text === r.origText) return;
						if (r.secret && r.text === MASK) return;
						changes[r.key] = parseValue(r.text, r.original);
					});
				} catch (e) {
					return toast(e.message, "error");
				}
				if (!Object.keys(changes).length && !remove.length) return toast(t("Nothing to change"), "info");
				var summary = Object.keys(changes).concat(remove).join(", ");
				if (!window.confirm(t("Apply config changes to {0}? ({1})", [this.stName, summary]))) return;
				this.saving = true;
				call("update_site_config", { site: this.stName, changes: changes, remove: remove }).then(
					function (r) {
						(r.warnings || []).forEach(function (w) { toast(t(w), "warning"); });
						toast(t("Job queued: {0}", [t("Update config")]), "success");
						var poll = nx().pollJob(r.job, function () {});
						poll.done.then(function (job) {
							if (job && job.status === "Success") {
								toast(t("Site config updated"), "success");
								self.loadConfig(true);
							} else if (job) {
								toast(job.error || t("Config update failed"), "error");
							}
						});
						if (typeof self.load === "function") self.load(true);
					},
					function (e) { toast(e.message, "error"); }
				).finally(function () { self.saving = false; });
			},
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxSiteTools", factory);
	});
})();
