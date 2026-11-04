/* Nexaploy portal: uptime monitors, incidents and the public status page (/dashboard/monitoring).
 *
 * Served as /assets/nexaploy/js/monitoring.js and included as a plain <script> in the head
 * block of www/dashboard/monitoring.html so Alpine.data("nxMonitoring") registers before Alpine
 * starts. API: nexaploy.api.monitoring.* (team read / write). Charts are inline SVG (no CDN).
 */
(function () {
	"use strict";

	var M = "nexaploy.api.monitoring.";
	var RANGES = [
		{ hours: 24, label: "24 hours" },
		{ hours: 168, label: "7 days" },
		{ hours: 720, label: "30 days" },
	];
	var CHART_W = 600;
	var CHART_H = 140;

	function nx() { return window.nx; }
	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }
	function call(method, args) { return nx().call(M + method, args); }
	function toast(msg, kind) { if (nx()) nx().toast(msg, kind); }

	function blankMonitor(site) {
		return {
			name: null, title: "", site: site ? site.name : "", hostname: site ? site.hosts[0] : "", path: "/",
			method: "GET", expected_status: "200-399", keyword: "", interval_minutes: 5, timeout_seconds: 10,
			failure_threshold: 2, probes: [], quorum: 0, enabled: true, is_default: false,
		};
	}

	/* Points of an hourly series -> SVG path (gaps break the line). */
	function chartPath(series, max) {
		if (!series || !series.length || !max) return "";
		var step = series.length > 1 ? CHART_W / (series.length - 1) : CHART_W;
		var d = "";
		var pen = false;
		series.forEach(function (p, i) {
			if (p.ms === null || p.ms === undefined) { pen = false; return; }
			var x = (i * step).toFixed(1);
			var y = (CHART_H - (p.ms / max) * (CHART_H - 8)).toFixed(1);
			d += (pen ? " L" : " M") + x + " " + y;
			pen = true;
		});
		return d.trim();
	}

	function factory() {
		return {
			loading: true,
			busy: false,
			error: null,
			tab: "monitors",
			data: null,
			form: null,
			detail: null,
			detailLoading: false,
			range: 24,
			ranges: RANGES,
			incidents: [],
			incidentsLoaded: false,
			openIncident: null,
			updateForm: { status: "Investigating", message: "" },
			page: null,

			init: function () {
				var self = this;
				this.load().then(function () {
					var wanted = nx().query("monitor");
					if (wanted) self.show(wanted);
				});
			},

			load: function () {
				var self = this;
				this.loading = true;
				return call("overview").then(function (r) {
					self.data = r;
					self.page = JSON.parse(JSON.stringify(r.status_page));
					self.error = null;
				}).catch(function (e) { self.error = e.message; }).finally(function () { self.loading = false; });
			},

			canWrite: function () { return !!(this.data && this.data.can_write); },
			sites: function () { return this.data ? this.data.sites : []; },
			site: function (name) { return this.sites().filter(function (s) { return s.name === name; })[0] || null; },
			monitors: function () { return this.data ? this.data.monitors : []; },
			probes: function () { return this.data ? this.data.probes : []; },

			statusClass: function (status) {
				return "badge " + ({ Up: "success", Down: "danger", Pending: "info", Paused: "" }[status] || "");
			},
			statusText: function (status) {
				return t({ Up: "Up", Down: "Down", Pending: "Pending", Paused: "Paused" }[status] || status || "");
			},
			pct: function (v) { return v === null || v === undefined ? "–" : Number(v).toFixed(2) + "%"; },
			ms: function (v) { return v === null || v === undefined ? "–" : v + " ms"; },
			tls: function (m) {
				if (m.tls_days_left === null || m.tls_days_left === undefined) return "";
				return t("{0} days", [m.tls_days_left]);
			},
			tlsClass: function (m) {
				if (m.tls_days_left === null || m.tls_days_left === undefined) return "";
				return m.tls_days_left < 7 ? "text-danger" : (m.tls_days_left < 14 ? "text-warning" : "muted");
			},
			intervalAllowed: function (minutes) {
				if (!this.data || !this.form) return true;
				var s = this.site(this.form.site);
				var lim = s && s.advanced ? this.data.limits.advanced : this.data.limits.basic;
				return minutes >= lim.min_interval;
			},
			siteLimit: function (siteName) {
				var s = this.site(siteName);
				if (!this.data || !s) return null;
				return (s.advanced ? this.data.limits.advanced : this.data.limits.basic).max_per_site;
			},

			/* ---------------------------------------------------------------- monitors */
			openNew: function () {
				var s = this.sites().filter(function (x) { return x.status === "Active"; })[0] || this.sites()[0];
				this.form = blankMonitor(s);
				this.detail = null;
			},
			edit: function (m) {
				this.form = {
					name: m.name, title: m.title, site: m.site, hostname: m.hostname, path: m.path || "/", method: m.method,
					expected_status: m.expected_status, keyword: m.keyword || "", interval_minutes: m.interval_minutes,
					timeout_seconds: m.timeout_seconds, failure_threshold: m.failure_threshold,
					probes: (m.probes || "").split(",").filter(Boolean), quorum: m.quorum || 0, enabled: !!m.enabled,
					is_default: !!m.is_default,
				};
			},
			cancel: function () { this.form = null; },
			onSiteChange: function () {
				var s = this.site(this.form.site);
				this.form.hostname = s && s.hosts.length ? s.hosts[0] : "";
			},
			toggleProbe: function (name) {
				var i = this.form.probes.indexOf(name);
				if (i === -1) this.form.probes.push(name);
				else this.form.probes.splice(i, 1);
			},
			save: function () {
				var self = this;
				var data = JSON.parse(JSON.stringify(this.form));
				data.enabled = data.enabled ? 1 : 0;
				data.probes = data.probes.join(",");
				this.busy = true;
				call("save_monitor", { monitor: data }).then(function () {
					toast(t("Monitor saved"), "success");
					self.form = null;
					return self.load();
				}).catch(function (e) { toast(e.message, "danger"); }).finally(function () { self.busy = false; });
			},
			toggleEnabled: function (m) {
				var self = this;
				this.edit(m);
				this.form.enabled = !m.enabled;
				var data = JSON.parse(JSON.stringify(this.form));
				data.enabled = data.enabled ? 1 : 0;
				data.probes = data.probes.join(",");
				this.form = null;
				call("save_monitor", { monitor: data }).then(function () { return self.load(); })
					.catch(function (e) { toast(e.message, "danger"); });
			},
			remove: function (m) {
				var self = this;
				if (!window.confirm(t("Delete the monitor {0}? Its history is deleted too.", [m.title]))) return;
				call("delete_monitor", { name: m.name }).then(function () {
					toast(t("Monitor deleted"), "success");
					if (self.detail && self.detail.monitor.name === m.name) self.detail = null;
					return self.load();
				}).catch(function (e) { toast(e.message, "danger"); });
			},

			/* ---------------------------------------------------------------- detail & chart */
			show: function (name, hours) {
				var self = this;
				if (hours) this.range = hours;
				this.form = null;
				this.detailLoading = true;
				return call("get_monitor", { name: name, hours: this.range }).then(function (r) {
					self.detail = r;
				}).catch(function (e) { toast(e.message, "danger"); }).finally(function () { self.detailLoading = false; });
			},
			closeDetail: function () { this.detail = null; },
			chartMax: function () {
				if (!this.detail) return 0;
				var max = 0;
				this.detail.series.forEach(function (p) { if (p.ms !== null && p.ms > max) max = p.ms; });
				if (!max) return 0;
				var nice = Math.pow(10, Math.floor(Math.log10(max)));
				return Math.ceil(max / nice) * nice;
			},
			chartPath: function () { return this.detail ? chartPath(this.detail.series, this.chartMax()) : ""; },
			/* vertical markers at hours with failed checks */
			chartFailPath: function () {
				if (!this.detail) return "";
				var n = this.detail.series.length;
				var step = n > 1 ? CHART_W / (n - 1) : CHART_W;
				var d = "";
				this.detail.series.forEach(function (p, i) { if (p.failures) d += " M" + (i * step).toFixed(1) + " 0 V" + CHART_H; });
				return d.trim();
			},
			chartAvg: function () {
				if (!this.detail) return null;
				var sum = 0, n = 0;
				this.detail.series.forEach(function (p) { if (p.ms !== null) { sum += p.ms; n += 1; } });
				return n ? Math.round(sum / n) : null;
			},
			chartW: CHART_W,
			chartH: CHART_H,
			barTitle: function (d) { return d.date + (d.uptime === null ? "" : ": " + Number(d.uptime).toFixed(2) + "%"); },
			probeState: function (p) {
				if (!p.state) return t("No result yet");
				return (p.state.ok ? t("OK") : (p.state.error || t("Failing"))) + (p.state.ms !== null && p.state.ms !== undefined ? " · " + p.state.ms + " ms" : "");
			},

			/* ---------------------------------------------------------------- incidents */
			showIncidents: function () {
				this.tab = "incidents";
				if (!this.incidentsLoaded) this.loadIncidents();
			},
			loadIncidents: function () {
				var self = this;
				return call("list_incidents", { limit: 100 }).then(function (rows) {
					self.incidents = rows;
					self.incidentsLoaded = true;
				}).catch(function (e) { toast(e.message, "danger"); });
			},
			duration: function (seconds) {
				if (seconds === null || seconds === undefined) return "";
				var s = Number(seconds);
				if (s < 60) return t("{0} s", [s]);
				if (s < 3600) return t("{0} min", [Math.round(s / 60)]);
				if (s < 172800) return t("{0} h", [(s / 3600).toFixed(1)]);
				return t("{0} d", [(s / 86400).toFixed(1)]);
			},
			toggleIncident: function (inc) {
				this.openIncident = this.openIncident === inc.name ? null : inc.name;
				this.updateForm = { status: inc.status === "Open" ? "Investigating" : "Resolved", message: "" };
			},
			saveIncident: function (inc) {
				var self = this;
				call("update_incident", { name: inc.name, public_title: inc.public_title || "", hide_from_status_page: inc.hide_from_status_page ? 1 : 0 })
					.then(function () { toast(t("Incident saved"), "success"); return self.loadIncidents(); })
					.catch(function (e) { toast(e.message, "danger"); });
			},
			postUpdate: function (inc) {
				var self = this;
				this.busy = true;
				call("add_incident_update", { name: inc.name, update: this.updateForm }).then(function () {
					toast(t("Update posted"), "success");
					self.updateForm = { status: self.updateForm.status, message: "" };
					return self.loadIncidents();
				}).catch(function (e) { toast(e.message, "danger"); }).finally(function () { self.busy = false; });
			},

			/* ---------------------------------------------------------------- status page */
			onPage: function (name) {
				return !!this.page && this.page.monitors.some(function (r) { return r.monitor === name; });
			},
			pageRow: function (name) {
				return this.page ? this.page.monitors.filter(function (r) { return r.monitor === name; })[0] : null;
			},
			togglePageMonitor: function (m) {
				var rows = this.page.monitors;
				var i = rows.map(function (r) { return r.monitor; }).indexOf(m.name);
				if (i === -1) rows.push({ monitor: m.name, display_name: m.is_default ? "" : m.title });
				else rows.splice(i, 1);
			},
			movePageMonitor: function (idx, delta) {
				var rows = this.page.monitors;
				var j = idx + delta;
				if (j < 0 || j >= rows.length) return;
				var tmp = rows[idx];
				rows.splice(idx, 1, rows[j]);
				rows.splice(j, 1, tmp);
			},
			monitorTitle: function (name) {
				var m = this.monitors().filter(function (x) { return x.name === name; })[0];
				return m ? m.title : name;
			},
			savePage: function () {
				var self = this;
				var data = JSON.parse(JSON.stringify(this.page));
				data.enabled = data.enabled ? 1 : 0;
				data.show_incident_history = data.show_incident_history ? 1 : 0;
				this.busy = true;
				call("save_status_page", { page: data }).then(function (r) {
					self.page = r;
					self.data.status_page = JSON.parse(JSON.stringify(r));
					toast(t("Status page saved"), "success");
				}).catch(function (e) { toast(e.message, "danger"); }).finally(function () { self.busy = false; });
			},
		};
	}

	document.addEventListener("alpine:init", function () {
		window.Alpine.data("nxMonitoring", factory);
	});
})();
