"""Server resource charts for the portal (CPU load, memory, disk over 24 h / 7 d / 30 d)."""

import frappe
from frappe.utils import add_to_date, now_datetime

from nexaploy import metrics_utils as mu
from nexaploy.api.dashboard import _check_team, _ctx


@frappe.whitelist()
def get_server_metrics(server, period="24h"):
	c = _ctx()
	_check_team("Server", server, c, "read")
	since = add_to_date(now_datetime(), days=-mu.period_days(period))
	rows = frappe.get_all(
		"Server Metric",
		filters={"server": server, "timestamp": [">=", since]},
		fields=["timestamp", "load_1", "memory_used_mb", "memory_total_mb", "disk_used_gb", "disk_total_gb"],
		order_by="timestamp asc",
		limit_page_length=0,
	)
	cpu_count = frappe.db.get_value("Server", server, "cpu_count")
	data = mu.build_series(rows, cpu_count)
	data.update({"server": server, "period": period if period in mu.PERIODS else mu.DEFAULT_PERIOD,
	             "cpu_count": cpu_count})
	return data
