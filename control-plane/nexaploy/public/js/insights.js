/* Nexaploy portal: site insights - analytics, logs, slow queries, SQL playground, console.
 *
 * Served as /assets/nexaploy/js/insights.js and included as a plain <script> in the head block of
 * www/dashboard/insights.html so Alpine.data("nxInsights") registers before Alpine starts.
 * Uses window.nx (dashboard.js) lazily, at call time.
 *
 * Every action queues an agent job via nexaploy.api.observability.* ({job}) and polls
 * get_result(job) until `done` (same pattern as nx.pollJob, but the result is team-checked with
 * the permission the action needs: read / write / admin).
 */
(function () {
	"use strict";

	var M = "nexaploy.api.observability.";
	var POLL_MS = 1500;
	var MAX_POLLS = 400; // ~10 minutes

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function query(name) { return nx() ? nx().query(name) : new URLSearchParams(location.search).get(name); }

	function fmtNum(n, digits) {
		if (n === null || n === undefined || n === "") return "—";
		var v = Number(n);
		if (!isFinite(v)) return "—";
		return v.toLocaleString(undefined, { maximumFractionDigits: digits === undefined ? 1 : digits });
	}

	function esc(s) {
		return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
			return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
		});
	}

	/* CSV (RFC 4180): quote every field that needs it; NULL -> empty. */
	function toCsv(columns, rows) {
		function cell(v) {
			if (v === null || v === undefined) return "";
			var s = String(v);
			return /[",\r\n]/.test(s) || /^\s|\s$/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
		}
		var lines = [columns.map(cell).join(",")];
		rows.forEach(function (r) { lines.push(r.map(cell).join(",")); });
		return lines.join("\r\n") + "\r\n";
	}

	/* Inline SVG bar chart (no chart library). points: [{label, value, err}] */
	function barChart(points, opts) {
		opts = opts || {};
		var w = 720, h = 160, pad = 18, n = points.length;
		if (!n) return '<div class="empty">' + esc(t("No data for this period.")) + "</div>";
		var max = points.reduce(function (a, p) { return Math.max(a, p.value || 0); }, 0) || 1;
		var bw = (w - pad) / n;
		var out = ['<svg viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" role="img" aria-label="' + esc(opts.label || "") + '">'];
		out.push('<line class="axis" x1="0" y1="' + (h - pad) + '" x2="' + w + '" y2="' + (h - pad) + '"/>');
		points.forEach(function (p, i) {
			var bh = Math.max(p.value ? 1 : 0, Math.round(((h - pad - 4) * (p.value || 0)) / max));
			var x = pad / 2 + i * bw;
			var title = esc(p.label + ": " + fmtNum(p.value, 0) + (p.err ? " (" + p.err + " 5xx)" : ""));
			out.push('<rect class="bar' + (p.err ? " err" : "") + '" x="' + x.toFixed(2) + '" y="' + (h - pad - bh) + '" width="' + Math.max(1, bw - 1).toFixed(2) + '" height="' + bh + '"><title>' + title + "</title></rect>");
		});
		out.push('<text x="2" y="10">' + esc(fmtNum(max, 0)) + "</text>");
		out.push('<text x="2" y="' + (h - 4) + '">' + esc(points[0].label) + "</text>");
		out.push('<text x="' + (w - 2) + '" y="' + (h - 4) + '" text-anchor="end">' + esc(points[n - 1].label) + "</text>");
		out.push("</svg>");
		return out.join("");
	}

	function panel() { return { loading: false, error: null, data: null, job: null, _timer: null }; }

	function factory() {
		return {
			site: null,
			perms: null,
			permError: null,
			tab: "analytics",
			analytics: Object.assign(panel(), { days: 7 }),
			logs: Object.assign(panel(), { log: "web", lines: 200 }),
			slow: panel(),
			sql: Object.assign(panel(), { query: "", readOnly: true, maxRows: 500 }),
			con: Object.assign(panel(), { code: "", commit: false }),

			init: function () {
				var self = this;
				this.site = query("site") || query("name");
				if (!this.site) return;
				var initial = query("tab");
				call("get_permissions", { site: this.site }).then(
					function (p) {
						self.perms = p;
						var allowed = ["analytics", "logs", "slow"].concat(p.sql ? ["sql"] : [], p.console ? ["console"] : []);
						self.show(allowed.indexOf(initial) !== -1 ? initial : "analytics");
					},
					function (e) { self.permError = e.message; }
				);
			},
			destroy: function () {
				[this.analytics, this.logs, this.slow, this.sql, this.con].forEach(function (p) { clearTimeout(p._timer); });
			},

			show: function (tab) {
				this.tab = tab;
				if (tab === "analytics" && !this.analytics.data && !this.analytics.loading) this.loadAnalytics(false);
				if (tab === "logs" && !this.logs.data && !this.logs.loading) this.loadLogs(false);
				if (tab === "slow" && !this.slow.data && !this.slow.loading) this.loadSlow(false);
			},

			/* ---------------------------------------------------------------- job plumbing */
			_run: function (p, method, args) {
				var self = this;
				clearTimeout(p._timer);
				p.loading = true;
				p.error = null;
				return call(method, args).then(
					function (r) { p.job = r.job; self._poll(p, r.job, 0); },
					function (e) { p.loading = false; p.error = e.message; }
				);
			},
			_poll: function (p, job, attempt) {
				var self = this;
				call("get_result", { job: job }).then(
					function (r) {
						if (p.job !== job) return; // superseded by a newer request
						if (r.done) {
							p.loading = false;
							p.error = r.status === "Failure" ? (r.error || t("The job failed")) : null;
							p.data = r.data || (r.status === "Failure" ? null : p.data);
							if (p.data && p.data.error) p.error = p.data.error;
							return;
						}
						if (attempt > MAX_POLLS) {
							p.loading = false;
							p.error = t("The server agent did not answer. Try again later.");
							return;
						}
						p._timer = setTimeout(function () { self._poll(p, job, attempt + 1); }, POLL_MS);
					},
					function (e) { p.loading = false; p.error = e.message; }
				);
			},

			/* ---------------------------------------------------------------- analytics */
			loadAnalytics: function (refresh) {
				return this._run(this.analytics, "get_analytics", { site: this.site, days: this.analytics.days, refresh: refresh ? 1 : 0 });
			},
			hourlyChart: function () {
				var d = this.analytics.data;
				if (!d) return "";
				var useDays = (d.hourly || []).length > 24 * 8;
				var src = useDays ? d.daily : d.hourly;
				var pts = (src || []).map(function (b) {
					var label = useDays ? b.date : String(b.hour).replace("T", " ").replace(":00Z", ":00");
					return { label: label, value: (b.requests || 0) + (b.jobs || 0), err: b.errors || 0 };
				});
				return barChart(pts, { label: t("Requests per hour") });
			},
			share: function (value, rows, key) {
				var max = (rows || []).reduce(function (a, r) { return Math.max(a, Number(r[key]) || 0); }, 0) || 1;
				return Math.max(2, Math.round((100 * (Number(value) || 0)) / max)) + "%";
			},
			statusBadge: function (code) {
				var c = String(code);
				return "badge " + (c[0] === "5" ? "danger" : c[0] === "4" ? "warning" : c[0] === "3" ? "info" : "success");
			},
			ms: function (v) {
				if (v === null || v === undefined) return "—";
				if (v >= 1000) return fmtNum(v / 1000, 2) + " " + t("s");
				return fmtNum(v, 0) + " " + t("ms");
			},
			secs: function (v) { return v === null || v === undefined ? "—" : fmtNum(v, 2) + " " + t("s"); },
			fmtNum: fmtNum,

			/* ---------------------------------------------------------------- logs / slow queries */
			loadLogs: function (refresh) {
				return this._run(this.logs, "get_logs", { site: this.site, log: this.logs.log, lines: this.logs.lines, refresh: refresh ? 1 : 0 });
			},
			loadSlow: function (refresh) {
				return this._run(this.slow, "get_slow_queries", { site: this.site, refresh: refresh ? 1 : 0 });
			},

			/* ---------------------------------------------------------------- SQL */
			runSql: function () {
				if (!this.sql.query.trim() || this.sql.loading) return;
				var ro = this.perms && this.perms.sql_write ? !!this.sql.readOnly : true;
				if (!ro && !window.confirm(t("Run this query in WRITE mode on {0}? Changes cannot be undone.", [this.site]))) return;
				this.sql.data = null;
				return this._run(this.sql, "run_sql", { site: this.site, query: this.sql.query, read_only: ro ? 1 : 0, max_rows: this.sql.maxRows });
			},
			sqlSummary: function () {
				var d = this.sql.data;
				if (!d) return "";
				var parts = [t("{0} statement(s)", [d.statements || 0]), t("{0} row(s)", [fmtNum(d.row_count, 0)])];
				if ((d.result_sets || []).length > 1) parts.push(t("showing the last of {0} result sets", [d.result_sets.length]));
				parts.push(d.read_only ? t("read-only") : t("write mode"));
				return parts.join(" · ");
			},
			downloadCsv: function () {
				var d = this.sql.data;
				if (!d || !d.columns.length) return;
				var blob = new Blob(["﻿" + toCsv(d.columns, d.rows)], { type: "text/csv;charset=utf-8" });
				var a = document.createElement("a");
				a.href = URL.createObjectURL(blob);
				a.download = this.site.replace(/[^a-z0-9.-]/gi, "_") + "-query.csv";
				document.body.appendChild(a);
				a.click();
				setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 0);
			},

			/* ---------------------------------------------------------------- console */
			runConsole: function () {
				if (!this.con.code.trim() || this.con.loading) return;
				if (this.con.commit && !window.confirm(t("Execute this code on {0} and COMMIT its database changes?", [this.site]))) return;
				this.con.data = null;
				return this._run(this.con, "run_console", { site: this.site, code: this.con.code, commit: this.con.commit ? 1 : 0 });
			},
		};
	}

	window.nxInsightsUtils = { toCsv: toCsv, barChart: barChart, esc: esc };

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxInsights", factory);
	});
})();
