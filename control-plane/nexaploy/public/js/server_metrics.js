/* Nexaploy portal: CPU / memory / disk charts of a server (inline SVG, no libraries).
 *
 * Served as /assets/nexaploy/js/server_metrics.js, loaded by www/dashboard/server.html as a
 * plain <script> so it registers before Alpine starts. Registers `nxServerMetrics(server)`.
 * API: nexaploy.api.server_metrics.get_server_metrics(server, period=24h|7d|30d).
 */
(function () {
	"use strict";
	if (window.__nxServerMetricsLoaded) return;
	window.__nxServerMetricsLoaded = true;

	var W = 600, H = 120, PAD = 4;
	var COLORS = { cpu: "var(--nx-primary, #2563eb)", memory: "#7c3aed", disk: "#0891b2" };

	function t(msg, args) { return typeof window.__ === "function" ? window.__(msg, args) : msg; }

	function path(points, key) {
		var n = points.length, d = "", started = false;
		if (!n) return "";
		for (var i = 0; i < n; i++) {
			var v = points[i][key];
			if (v === null || v === undefined) { started = false; continue; }
			var x = n === 1 ? W / 2 : PAD + (i * (W - 2 * PAD)) / (n - 1);
			var y = H - PAD - (Math.max(0, Math.min(100, v)) * (H - 2 * PAD)) / 100;
			d += (started ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1) + " ";
			started = true;
		}
		return d.trim();
	}

	function area(points, key) {
		var line = path(points, key);
		if (!line || line.indexOf("L") < 0) return "";
		var xs = line.match(/[ML]([\d.]+) /g) || [];
		var first = parseFloat(xs[0].slice(1)), last = parseFloat(xs[xs.length - 1].slice(1));
		return line + " L" + last + " " + (H - PAD) + " L" + first + " " + (H - PAD) + " Z";
	}

	function register() {
		window.Alpine.data("nxServerMetrics", function (server) {
			return {
				server: server, period: "24h", data: null, loading: false, error: null, W: W, H: H,
				metrics: [
					{ key: "cpu", label: t("CPU load"), color: COLORS.cpu },
					{ key: "memory", label: t("Memory"), color: COLORS.memory },
					{ key: "disk", label: t("Disk"), color: COLORS.disk },
				],
				init: function () { this.load(); },
				load: function () {
					var self = this;
					self.loading = true; self.error = null;
					window.nx.call("nexaploy.api.server_metrics.get_server_metrics", { server: self.server, period: self.period })
						.then(function (r) { self.data = r; })
						.catch(function (e) { self.error = (e && e.message) || t("Could not load the charts"); })
						.finally(function () { self.loading = false; });
				},
				setPeriod: function (p) { if (p !== this.period) { this.period = p; this.load(); } },
				line: function (key) { return this.data ? path(this.data.points, key) : ""; },
				fill: function (key) { return this.data ? area(this.data.points, key) : ""; },
				stat: function (key, which) {
					var s = this.data && this.data.summary && this.data.summary[key];
					var v = s ? s[which] : null;
					return v === null || v === undefined ? "—" : v + "%";
				},
				warn: function (key) {
					var s = this.data && this.data.summary && this.data.summary[key];
					return !!(s && s.current !== null && s.current >= (key === "disk" ? 85 : 90));
				},
				range: function () {
					var p = this.data && this.data.points;
					if (!p || !p.length) return "";
					return window.nx.fmtDate(p[0].t) + " → " + window.nx.fmtDate(p[p.length - 1].t);
				},
			};
		});
	}

	if (window.Alpine) register();
	else document.addEventListener("alpine:init", register);
})();
