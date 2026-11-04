"""Server resource charts: metrics_utils (pure) + API/page wiring."""

import os
import subprocess
from datetime import datetime, timedelta

from nexaploy import metrics_utils as mu

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nexaploy")
T0 = datetime(2026, 11, 4, 0, 0)


def row(i, load=1.0, mem=(2048, 4096), disk=(40, 80)):
	return {"timestamp": T0 + timedelta(minutes=5 * i), "load_1": load, "memory_used_mb": mem[0],
	        "memory_total_mb": mem[1], "disk_used_gb": disk[0], "disk_total_gb": disk[1]}


def test_point_percentages_and_clamping():
	p = mu.point(row(0, load=1.0), cpu_count=4)
	assert (p["cpu"], p["memory"], p["disk"]) == (25.0, 50.0, 50.0)
	assert mu.point(row(0, load=9.0), cpu_count=2)["cpu"] == 100.0  # overload clamps at 100
	assert mu.point(row(0, mem=(1, 0)), cpu_count=0)["memory"] is None  # unknown total
	assert mu.point(row(0), cpu_count=None)["cpu"] == 100.0  # cpus default to 1


def test_downsample_and_summary():
	rows = [row(i, load=(4.0 if i == 7 else 1.0)) for i in range(1000)]
	data = mu.build_series(rows, cpu_count=4, max_points=100)
	assert data["samples"] == 1000 and len(data["points"]) <= 100
	assert data["summary"]["cpu"]["max"] == 100.0  # the peak survives downsampling
	assert data["summary"]["cpu"]["current"] == 25.0
	assert data["points"][0]["t"] == T0.isoformat()


def test_empty_and_periods():
	assert mu.build_series([], 2) == {"points": [], "summary": {k: {"current": None, "avg": None, "max": None}
	                                                             for k in ("cpu", "memory", "disk")}, "samples": 0}
	assert mu.period_days("7d") == 7 and mu.period_days("30d") == 30 and mu.period_days("x") == 1


def test_metrics_utils_is_frappe_free():
	assert "import frappe" not in open(os.path.join(APP, "metrics_utils.py")).read()


def test_api_checks_team_access():
	src = open(os.path.join(APP, "api", "server_metrics.py")).read()
	assert "_ctx()" in src and '_check_team("Server", server, c, "read")' in src
	assert "@frappe.whitelist()" in src and "allow_guest" not in src


def test_server_page_wiring_and_js():
	html = open(os.path.join(APP, "www", "dashboard", "server.html")).read()
	assert "/assets/nexaploy/js/server_metrics.js" in html and 'x-data="nxServerMetrics(server.name)"' in html
	assert html.count("<template") == html.count("</template>")
	js = os.path.join(APP, "public", "js", "server_metrics.js")
	assert "nexaploy.api.server_metrics.get_server_metrics" in open(js).read()
	assert "cdn" not in open(js).read().lower()
	subprocess.run(["node", "--check", js], check=True)


def test_svg_viewbox_is_a_literal_attribute():
	"""The HTML parser lowercases attribute names, so an Alpine `:viewBox` binding sets
	`viewbox`, which SVG ignores (charts drew in raw pixels, ~60% of the width). The JS draws
	in a 600x120 box: the template must declare exactly that, literally."""
	html = open(os.path.join(APP, "www", "dashboard", "server.html")).read()
	assert ":viewBox" not in html and 'viewBox="0 0 600 120"' in html
	assert "var W = 600, H = 120" in open(os.path.join(APP, "public", "js", "server_metrics.js")).read()
	for root, _d, files in os.walk(os.path.join(APP, "www")):
		for name in files:
			if name.endswith(".html"):
				assert ":viewBox" not in open(os.path.join(root, name)).read(), name
	mon = open(os.path.join(APP, "www", "dashboard", "monitoring.html")).read()
	js_mon = open(os.path.join(APP, "public", "js", "monitoring.js")).read()
	assert 'viewBox="0 0 600 140"' in mon and "CHART_W = 600" in js_mon and "CHART_H = 140" in js_mon
