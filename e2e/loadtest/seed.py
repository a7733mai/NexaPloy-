"""Create N load-test Servers in a control plane site and print their tokens as JSON.

Runs inside the control plane backend container (Frappe's env python):

  docker exec -i <bench>-backend bash -c 'cd /home/frappe/frappe-bench/sites && \
      ../env/bin/python - <site> <count>' < e2e/loadtest/seed.py > tokens.json
  ... --cleanup removes them again (server_id prefix "lt-").

Servers belong to a billing-exempt team so no subscriptions are created.
"""
import hashlib
import json
import secrets
import sys

import frappe

site, count = sys.argv[1], sys.argv[2]
frappe.init(site=site, sites_path=".")
frappe.connect()
frappe.set_user("Administrator")
PREFIX = "lt-"

if count == "--cleanup":
	names = frappe.get_all("Server", filters={"server_id": ["like", PREFIX + "%"]}, pluck="name")
	for table in ("Server Metric", "Agent Job", "Site Usage"):
		try:
			frappe.db.delete(table, {"server": ["in", names or [""]]})
		except Exception:
			pass
	frappe.db.delete("Server", {"name": ["in", names or [""]]})
	frappe.db.commit()
	print(json.dumps({"deleted": len(names)}))
	sys.exit(0)

team = frappe.db.get_value("Team", {"billing_exempt": 1}, "name")
if not team:
	from nexaploy.teams import create_team

	team = create_team("Load test", owner="Administrator").name
	frappe.db.set_value("Team", team, "billing_exempt", 1)
out = []
for i in range(int(count)):
	server_id = f"{PREFIX}{i:05d}"
	token = secrets.token_urlsafe(32)
	if not frappe.db.exists("Server", server_id):
		frappe.get_doc({
			"doctype": "Server", "server_id": server_id, "hostname": f"{server_id}.loadtest",
			"ownership": "BYOS", "status": "Active", "team": team,
			"token_hash": hashlib.sha256(token.encode()).hexdigest(),
		}).insert(ignore_permissions=True)
	else:
		frappe.db.set_value("Server", server_id, "token_hash", hashlib.sha256(token.encode()).hexdigest())
	out.append({"server_id": server_id, "token": token})
frappe.db.commit()
print(json.dumps(out))
