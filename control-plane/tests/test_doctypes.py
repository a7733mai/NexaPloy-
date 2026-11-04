"""Static checks of the Frappe app layout and DocType JSON (no frappe needed)."""

import ast
import json
import os
import re
import tomllib

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "nexaploy")
DOCTYPE_DIR = os.path.join(APP, "nexaploy", "doctype")

EXPECTED = {
	"Server", "Server Registration Token", "Marketplace App", "Marketplace App Release",
	"Release Group", "Release Group App", "Deploy Candidate", "Bench", "Site", "Site Domain",
	"Site App", "Site Backup", "Agent Job", "Agent Job Step", "Nexaploy Plan",
	# billing
	"Billing Settings", "Nexaploy Subscription", "Nexaploy Invoice", "Nexaploy Invoice Item",
	"Nexaploy Payment",
	# operations
	"Nexaploy Settings", "Server Metric",
	# teams (§9)
	"Team", "Team Member", "Team Invitation",
	# custom apps from GitHub
	"GitHub Settings", "GitHub Installation", "Custom App",
	# plan usage / backup downloads
	"Site Usage", "Site Backup File",
	# site lifecycle (§10)
	"Site Transfer",
	# backup destinations (S3 / Google Drive / OneDrive / Dropbox)
	"Backup Destination",
}
CHILD_TABLES = {
	"Marketplace App Release", "Release Group App", "Site Domain", "Site App", "Agent Job Step",
	"Nexaploy Invoice Item",
	"Team Member",
	"Site Backup File",
}
REQUIRED_KEYS = {"doctype", "name", "module", "fields", "permissions", "modified", "istable", "engine"}
VALID_FIELDTYPES = {
	"Data", "Select", "Link", "Dynamic Link", "Check", "Int", "Float", "Currency", "Datetime", "Date",
	"JSON", "Long Text", "Small Text", "Text", "Text Editor", "Code", "Password", "Table",
	"Section Break", "Column Break", "Tab Break", "Read Only",
}
LAYOUT = {"Section Break", "Column Break", "Tab Break"}
# Frappe's reserved/default columns cannot be redefined as fields.
RESERVED = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "parent",
            "parentfield", "parenttype", "doctype"}


def snake(name):
	return name.lower().replace(" ", "_")


def load_all():
	docs = {}
	for folder in sorted(os.listdir(DOCTYPE_DIR)):
		path = os.path.join(DOCTYPE_DIR, folder, folder + ".json")
		if os.path.isfile(path):
			with open(path) as fh:
				docs[folder] = json.load(fh)
	return docs


DOCS = load_all()
BY_NAME = {d["name"]: d for d in DOCS.values()}


def test_all_doctypes_present():
	assert set(BY_NAME) == EXPECTED


@pytest.mark.parametrize("folder", sorted(DOCS))
def test_doctype_json(folder):
	d = DOCS[folder]
	missing = REQUIRED_KEYS - set(d)
	assert not missing, f"{folder}: missing {missing}"
	assert d["doctype"] == "DocType"
	assert d["module"] == "Nexaploy"
	assert snake(d["name"]) == folder
	base = os.path.join(DOCTYPE_DIR, folder)
	assert os.path.isfile(os.path.join(base, "__init__.py"))
	assert os.path.isfile(os.path.join(base, folder + ".py"))

	names = [f["fieldname"] for f in d["fields"]]
	assert len(names) == len(set(names)), f"{folder}: duplicate fieldnames"
	assert d.get("field_order", names) == names
	for f in d["fields"]:
		assert re.match(r"^[a-z][a-z0-9_]*$", f["fieldname"]), f["fieldname"]
		assert f["fieldname"] not in RESERVED, f"{folder}: reserved fieldname {f['fieldname']}"
		assert f["fieldtype"] in VALID_FIELDTYPES, f"{folder}.{f['fieldname']}: {f['fieldtype']}"
		if f["fieldtype"] not in LAYOUT:
			assert f.get("label"), f"{folder}.{f['fieldname']} has no label"
		if f["fieldtype"] in ("Link", "Table"):
			assert f.get("options"), f"{folder}.{f['fieldname']} needs options"
		if f["fieldtype"] == "Dynamic Link":
			assert f.get("options") in names
		for flag in ("reqd", "unique", "in_list_view", "read_only", "hidden"):
			assert f.get(flag, 0) in (0, 1)

	autoname = d.get("autoname", "")
	if autoname.startswith("field:"):
		assert autoname[len("field:"):] in names

	if d["istable"]:
		assert d["permissions"] == []
	else:
		roles = {p["role"] for p in d["permissions"]}
		assert "System Manager" in roles


def test_child_tables():
	for name in CHILD_TABLES:
		assert BY_NAME[name]["istable"] == 1
	for name in EXPECTED - CHILD_TABLES:
		assert BY_NAME[name]["istable"] == 0
	used = set()
	for d in BY_NAME.values():
		for f in d["fields"]:
			if f["fieldtype"] == "Table":
				target = f["options"]
				assert target in BY_NAME, f"{d['name']}.{f['fieldname']} -> unknown {target}"
				assert BY_NAME[target]["istable"] == 1
				used.add(target)
	assert used == CHILD_TABLES


def test_links_resolve():
	frappe_core = {"User", "DocType", "Currency"}
	for d in BY_NAME.values():
		for f in d["fields"]:
			if f["fieldtype"] == "Link":
				assert f["options"] in BY_NAME or f["options"] in frappe_core, f"{d['name']}.{f['fieldname']}"
			if f.get("fetch_from"):
				link, _, target = f["fetch_from"].partition(".")
				link_field = next(x for x in d["fields"] if x["fieldname"] == link)
				linked = BY_NAME.get(link_field["options"])
				assert linked and any(x["fieldname"] == target for x in linked["fields"])


def test_spec_fields_present():
	"""Key fields from ARCHITECTURE.md §8."""
	spec = {
		"Server": "server_id hostname ownership team status token_hash last_heartbeat facts plan",
		"Server Registration Token": "token_hash team expires_on used server",
		"Marketplace App": "app_name title repo_url publisher category description pricing price_monthly approved releases",
		"Marketplace App Release": "frappe_major branch commit",
		"Release Group": "title frappe_major frappe_branch team apps build_server",
		"Release Group App": "app branch",
		"Deploy Candidate": "release_group build_id image_tag apps_json status build_log",
		"Bench": "bench_name server release_group image_tag status",
		"Site": "site_name bench server team status plan domains apps admin_password",
		"Site Backup": "site remote_path with_files status size created_at",
		"Agent Job": "server kind params status reference_doctype reference_name error result steps",
		"Nexaploy Plan": "title plan_type price_monthly currency max_sites",
	}
	for dt, fields in spec.items():
		names = {f["fieldname"] for f in BY_NAME[dt]["fields"]}
		missing = set(fields.split()) - names
		assert not missing, f"{dt}: missing {missing}"
	server_id = next(f for f in BY_NAME["Server"]["fields"] if f["fieldname"] == "server_id")
	assert server_id.get("unique") == 1
	status = next(f for f in BY_NAME["Agent Job"]["fields"] if f["fieldname"] == "status")
	assert status["options"].split("\n") == ["Pending", "Queued", "Running", "Success", "Failure"]


def test_agent_job_kinds_match_helper():
	tree = ast.parse(open(os.path.join(APP, "agent_job.py")).read())
	kinds = None
	for node in tree.body:
		if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "JOB_KINDS":
			kinds = ast.literal_eval(node.value)
	kind_field = next(f for f in BY_NAME["Agent Job"]["fields"] if f["fieldname"] == "kind")
	assert tuple(kind_field["options"].split("\n")) == kinds


def test_callbacks_cover_kinds():
	src = open(os.path.join(APP, "agent_callbacks.py")).read()
	for kind in ("image.build", "bench.create", "site.create", "site.backup", "site.restore",
	             "site.drop", "site.install_app", "site.add_domain", "site.remove_domain", "site.migrate"):
		assert f'"{kind}"' in src, kind


def test_app_layout():
	with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
		py = tomllib.load(fh)
	assert py["project"]["name"] == "nexaploy"
	assert py["build-system"]["build-backend"] == "flit_core.buildapi"
	assert "version" in py["project"]["dynamic"]
	init = open(os.path.join(APP, "__init__.py")).read()
	assert re.search(r'^__version__ = "[\d.]+"', init, re.M)
	assert open(os.path.join(APP, "modules.txt")).read().strip() == "Nexaploy"
	assert os.path.isfile(os.path.join(APP, "patches.txt"))
	assert os.path.isdir(os.path.join(APP, "config"))
	hooks = open(os.path.join(APP, "hooks.py")).read()
	assert 'app_name = "nexaploy"' in hooks
	# whitelisted agent endpoints exist with the §5.6 names
	agent_src = open(os.path.join(APP, "api", "agent.py")).read()
	for fn in ("register", "heartbeat", "poll", "job_update"):
		assert re.search(rf"@frappe\.whitelist\(allow_guest=True[^)]*\)\ndef {fn}\(", agent_src), fn


def test_hook_targets_exist():
	"""Every dotted path referenced in hooks.py points at a function defined in the app."""
	hooks_src = open(os.path.join(APP, "hooks.py")).read()
	paths = set(re.findall(r'"(nexaploy\.[a-z_.]+)"', hooks_src))
	assert paths
	for dotted in paths:
		module, _, func = dotted.rpartition(".")
		file = os.path.join(ROOT, *module.split(".")) + ".py"
		assert os.path.isfile(file), dotted
		tree = ast.parse(open(file).read())
		assert any(isinstance(n, ast.FunctionDef) and n.name == func for n in tree.body), dotted


def test_controllers_define_class():
	for folder, d in DOCS.items():
		src = open(os.path.join(DOCTYPE_DIR, folder, folder + ".py")).read()
		cls = d["name"].replace(" ", "")
		assert re.search(rf"^class {cls}\(Document\):", src, re.M), folder


def test_fixture_role():
	with open(os.path.join(APP, "fixtures", "role.json")) as fh:
		roles = json.load(fh)
	assert roles[0]["role_name"] == "Nexaploy Customer"


def test_team_fields_link_team():
	"""§9: every `team` field in every DocType is a Link to Team."""
	found = 0
	for d in BY_NAME.values():
		for f in d["fields"]:
			if f["fieldname"] == "team":
				found += 1
				assert f["fieldtype"] == "Link" and f.get("options") == "Team", d["name"]
	assert found >= 9


def test_team_doctypes_spec():
	"""Key fields from ARCHITECTURE.md §9."""
	spec = {
		"Team": "team_name title owner_user billing_email currency enabled two_factor_required members",
		"Team Member": "user role",
		"Team Invitation": "team email role token_hash expires_on status",
	}
	for dt, fields in spec.items():
		names = {f["fieldname"] for f in BY_NAME[dt]["fields"]}
		missing = set(fields.split()) - names
		assert not missing, f"{dt}: missing {missing}"
	fields = {dt: {f["fieldname"]: f for f in BY_NAME[dt]["fields"]} for dt in spec}
	assert BY_NAME["Team"]["autoname"] == "field:team_name"
	assert fields["Team"]["team_name"].get("unique") == 1
	assert fields["Team"]["members"]["options"] == "Team Member"
	roles = ["Owner", "Admin", "Developer", "Billing"]
	assert fields["Team Member"]["role"]["options"].split("\n") == roles
	assert fields["Team Invitation"]["role"]["options"].split("\n") == roles
	assert fields["Team Invitation"]["status"]["options"].split("\n") == ["Pending", "Accepted", "Expired", "Revoked"]
	token = fields["Team Invitation"]["token_hash"]
	assert token.get("hidden") == 1 and token.get("unique") == 1
	for dt in ("Team", "Team Invitation"):
		perms = {p["role"]: p for p in BY_NAME[dt]["permissions"]}
		assert all(perms["System Manager"].get(k) for k in ("read", "write", "create", "delete"))
		customer = perms["Nexaploy Customer"]
		assert customer.get("read") == 1
		assert not any(customer.get(k) for k in ("write", "create", "delete"))


# activity log (§10) and prepaid credits: register the new DocTypes with the checks above
EXPECTED.update({"Team Activity", "Team Credit Transaction"})


def test_activity_and_credit_doctypes_link_team():
	for dt in ("Team Activity", "Team Credit Transaction"):
		team = next(f for f in BY_NAME[dt]["fields"] if f["fieldname"] == "team")
		assert team["fieldtype"] == "Link" and team["options"] == "Team" and team.get("reqd") == 1


# hosted server provisioning + SaaS signup: register the new DocTypes with the checks above
EXPECTED.update({"Cloud Provider", "Cloud Region", "Cloud Server Size", "SaaS Product", "SaaS Signup"})


def test_provisioning_and_saas_doctypes():
	fields = {dt: {f["fieldname"]: f for f in BY_NAME[dt]["fields"]} for dt in BY_NAME}
	server = fields["Server"]
	for name in ("provider", "region", "size", "provider_server_id", "public_ipv4", "public_ipv6", "provisioning_status"):
		assert name in server, name
	assert server["provider"]["options"] == "Cloud Provider"
	assert server["region"]["options"] == "Cloud Region"
	assert server["size"]["options"] == "Cloud Server Size"
	assert server["provisioning_status"]["options"].split("\n") == [
		"Not Provisioned", "Creating", "Bootstrapping", "Ready", "Failed", "Deleting", "Deleted"
	]
	assert fields["Cloud Provider"]["api_token"]["fieldtype"] == "Password"
	assert fields["Cloud Provider"]["provider"]["options"].split("\n") == ["Hetzner", "DigitalOcean"]
	assert fields["Cloud Server Size"]["plan"]["options"] == "Nexaploy Plan"
	assert fields["Server Registration Token"]["server"]["options"] == "Server"
	assert fields["Site"]["trial_end_date"]["fieldtype"] == "Date"
	product = fields["SaaS Product"]
	assert BY_NAME["SaaS Product"]["autoname"] == "field:slug"
	assert product["release_group"]["options"] == "Release Group"
	assert product["default_plan"]["options"] == "Nexaploy Plan"
	assert product["trial_days"].get("default") == "14"
	signup = fields["SaaS Signup"]
	assert signup["status"]["options"].split("\n") == [
		"Pending Verification", "Verified", "Provisioning", "Active", "Failed"
	]
	for secret in ("token_hash", "status_token_hash"):
		assert signup[secret].get("hidden") == 1 and signup[secret].get("unique") == 1
	assert signup["password"]["fieldtype"] == "Password"
	# platform-managed DocTypes: customers reach them only through the portal APIs
	for dt in ("Cloud Provider", "Cloud Region", "Cloud Server Size", "SaaS Product", "SaaS Signup"):
		assert {p["role"] for p in BY_NAME[dt]["permissions"]} == {"System Manager"}, dt


# marketplace publishers / payouts and support tickets: register the new DocTypes
EXPECTED.update({"Marketplace Publisher", "Publisher Payout", "Publisher Payout Item", "Support Ticket", "Support Ticket Reply"})
CHILD_TABLES.update({"Publisher Payout Item", "Support Ticket Reply"})
VALID_FIELDTYPES.add("Attach Image")  # Marketplace App.logo


def test_publisher_and_support_doctypes_link_team():
	for dt in ("Marketplace Publisher", "Support Ticket"):
		team = next(f for f in BY_NAME[dt]["fields"] if f["fieldname"] == "team")
		assert team["fieldtype"] == "Link" and team["options"] == "Team" and team.get("reqd") == 1
	payout_team = next(f for f in BY_NAME["Publisher Payout"]["fields"] if f["fieldname"] == "team")
	assert payout_team.get("fetch_from") == "publisher.team"


def test_single_doctypes_have_no_link_defaults():
	"""Frappe's installer saves every Single with its defaults (init_singles) BEFORE
	after_install runs, so a Link default pointing at a record created in after_install
	breaks `bench install-app` (found on a real bench)."""
	import glob
	import json
	import os

	here = os.path.dirname(__file__)
	for path in glob.glob(os.path.join(here, "..", "nexaploy", "nexaploy", "doctype", "*", "*.json")):
		d = json.load(open(path))
		if d.get("doctype") != "DocType" or not d.get("issingle"):
			continue
		for f in d["fields"]:
			if f.get("fieldtype") in ("Link", "Dynamic Link"):
				assert not f.get("default"), f"{d['name']}.{f['fieldname']} has a Link default"


def test_no_for_update_in_get_all():
	"""frappe.get_all/get_list have no `for_update` in v15 (TypeError at runtime, found on a
	real bench): use frappe.db.get_value(..., for_update=True) or frappe.qb ... .for_update()."""
	import os
	import re

	app = os.path.join(os.path.dirname(__file__), "..", "nexaploy")
	call = re.compile(r"frappe\.get_(?:all|list)\((?:[^()]|\([^()]*\))*?for_update", re.S)
	for root, _dirs, files in os.walk(app):
		for name in files:
			if name.endswith(".py"):
				text = open(os.path.join(root, name)).read()
				assert not call.search(text), os.path.join(root, name)
