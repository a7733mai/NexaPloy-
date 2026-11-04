"""Hosted server provisioning (Hetzner / DigitalOcean) and SaaS signup: pure logic, no frappe."""

import ast
import csv
import glob
import os
import re
import shlex
from datetime import date, datetime, timedelta

import pytest
import yaml

from nexaploy import signup_utils as su
from nexaploy.provisioning import base, cloud_init, digitalocean, hetzner
from nexaploy.provisioning import utils as pu

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")
TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd"
CP = "https://cp.example.com"


def read(*parts):
	with open(os.path.join(APP, *parts), encoding="utf-8") as fh:
		return fh.read()


class FakeTransport:
	"""Records requests; answers from a {(method, path-suffix): (status, data)} table."""

	def __init__(self, responses=None):
		self.responses = responses or {}
		self.calls = []

	def __call__(self, method, url, headers, body, timeout):
		self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout})
		for (m, suffix), answer in self.responses.items():
			if m == method and url.split("?")[0].endswith(suffix):
				return answer
		return 200, {}


# --------------------------------------------------------------------------- purity


@pytest.mark.parametrize(
	"path",
	["provisioning/base.py", "provisioning/hetzner.py", "provisioning/digitalocean.py",
	 "provisioning/cloud_init.py", "provisioning/utils.py", "signup_utils.py"],
)
def test_pure_modules_do_not_import_frappe_or_requests(path):
	tree = ast.parse(read(*path.split("/")))
	for node in tree.body:  # module level only: requests is imported lazily inside a function
		if isinstance(node, ast.Import):
			assert all(not a.name.startswith(("frappe", "requests")) for a in node.names), path
		if isinstance(node, ast.ImportFrom):
			assert not (node.module or "").startswith(("frappe", "requests")), path


# --------------------------------------------------------------------------- cloud-init


def test_install_command():
	cmd = cloud_init.install_command(CP + "/", TOKEN, "ops@example.com")
	assert cmd.startswith(f"curl -fsSL {CP}/install.sh | bash -s -- ")
	args = shlex.split(cmd.split("|", 1)[1])
	assert args[args.index("--token") + 1] == TOKEN
	assert args[args.index("--control-plane") + 1] == CP
	assert args[args.index("--acme-email") + 1] == "ops@example.com"
	assert "--acme-email" not in cloud_init.install_command(CP, TOKEN)


@pytest.mark.parametrize(
	"cp,token,email",
	[
		("ftp://cp.example.com", TOKEN, None),
		("https://cp.example.com; rm -rf /", TOKEN, None),
		(CP, "short", None),
		(CP, TOKEN + "$(id)", None),
		(CP, TOKEN, "not-an-email"),
		(CP, TOKEN, "a@b.com'; reboot #"),
	],
)
def test_install_command_rejects_unsafe_input(cp, token, email):
	with pytest.raises(ValueError):
		cloud_init.install_command(cp, token, email)


def test_cloud_init_document():
	text = cloud_init.build_cloud_init(CP, TOKEN, "ops@example.com", hostname="erp-1", ssh_authorized_keys=["ssh-ed25519 AAAA ops"])
	assert text.startswith("#cloud-config\n")
	doc = yaml.safe_load(text)
	assert doc["hostname"] == "erp-1"
	assert "curl" in doc["packages"]
	assert doc["ssh_authorized_keys"] == ["ssh-ed25519 AAAA ops"]
	(cmd,) = doc["runcmd"]
	assert cmd[0] == "bash" and cmd[1] == "-c"
	script = cmd[2]
	assert f"curl -fsSL {CP}/install.sh | bash -s -- --token {TOKEN} --control-plane {CP} --acme-email ops@example.com" in script
	assert cloud_init.INSTALL_LOG in script
	assert "sudo" not in script  # cloud-init runs as root


def test_cloud_init_minimal_and_hostname_validation():
	doc = yaml.safe_load(cloud_init.build_cloud_init(CP, TOKEN))
	assert "hostname" not in doc and "ssh_authorized_keys" not in doc
	with pytest.raises(ValueError):
		cloud_init.build_cloud_init(CP, TOKEN, hostname="Bad_Host")


# --------------------------------------------------------------------------- providers


def test_get_provider_and_token_required():
	t = FakeTransport()
	assert isinstance(base.get_provider("Hetzner", "tok", t), hetzner.HetznerProvider)
	assert isinstance(base.get_provider("DigitalOcean", "tok", t), digitalocean.DigitalOceanProvider)
	with pytest.raises(base.ProviderError):
		base.get_provider("Linode", "tok", t)
	with pytest.raises(base.ProviderError):
		base.get_provider("Hetzner", "", t)


def test_parse_ssh_keys():
	assert base.parse_ssh_keys(None) == []
	assert base.parse_ssh_keys("123, 456\nops-key 123") == ["123", "456", "ops-key"]
	assert base.parse_ssh_keys(["1", " 2 "]) == ["1", "2"]


def test_hetzner_create_server_request():
	t = FakeTransport(
		{
			("POST", "/servers"): (
				201,
				{
					"server": {
						"id": 4711,
						"status": "initializing",
						"public_net": {"ipv4": {"ip": "1.2.3.4"}, "ipv6": {"ip": "2a01:4f8:c0c:1::/64"}},
					},
					"root_password": None,
				},
			)
		}
	)
	p = base.get_provider("Hetzner", "secret-token", t)
	result = p.create_server("erp-1", "fsn1", "cx32", "ubuntu-24.04", "#cloud-config\n", ["123", "ops"], {"nexaploy": "1"})
	assert result == {"id": "4711", "ipv4": "1.2.3.4", "ipv6": "2a01:4f8:c0c:1::1", "status": "initializing"}
	(call,) = t.calls
	assert call["method"] == "POST" and call["url"] == "https://api.hetzner.cloud/v1/servers"
	assert call["headers"]["Authorization"] == "Bearer secret-token"
	body = call["body"]
	assert body["name"] == "erp-1" and body["server_type"] == "cx32" and body["location"] == "fsn1"
	assert body["image"] == "ubuntu-24.04" and body["user_data"] == "#cloud-config\n"
	assert body["ssh_keys"] == [123, "ops"]
	assert body["labels"] == {"nexaploy": "1"}
	assert body["public_net"] == {"enable_ipv4": True, "enable_ipv6": True}


def test_hetzner_get_delete_and_errors():
	t = FakeTransport(
		{
			("GET", "/servers/1"): (200, {"server": {"id": 1, "status": "running", "public_net": {"ipv4": {"ip": "5.6.7.8"}, "ipv6": None}}}),
			("GET", "/servers/2"): (404, {"error": {"code": "not_found", "message": "server not found"}}),
			("DELETE", "/servers/2"): (404, {"error": {"code": "not_found"}}),
			("DELETE", "/servers/3"): (200, {"action": {"id": 9}}),
			("POST", "/servers"): (422, {"error": {"code": "invalid_input", "message": "invalid server_type"}}),
		}
	)
	p = hetzner.HetznerProvider("tok", t)
	assert p.get_server("1") == {"id": "1", "ipv4": "5.6.7.8", "ipv6": None, "status": "running"}
	assert p.get_server("2") is None
	assert p.delete_server("2") is True and p.delete_server("3") is True
	assert [c["method"] for c in t.calls[-2:]] == ["DELETE", "DELETE"]
	with pytest.raises(base.ProviderError) as exc:
		p.create_server("x", "fsn1", "nope", "ubuntu-24.04", "#cloud-config\n")
	assert exc.value.status == 422 and "invalid server_type" in str(exc.value)


def test_hetzner_catalog():
	t = FakeTransport(
		{
			("GET", "/locations"): (200, {"locations": [{"name": "fsn1", "city": "Falkenstein", "country": "DE"}]}),
			("GET", "/server_types"): (
				200,
				{
					"server_types": [
						{"name": "cx32", "cores": 4, "memory": 8.0, "disk": 80, "deprecated": False,
						 "prices": [{"location": "fsn1", "price_monthly": {"gross": "7.0", "net": "5.9"}},
						            {"location": "hel1", "price_monthly": {"gross": "6.8"}}]},
						{"name": "cx11", "cores": 1, "memory": 2.0, "disk": 20, "deprecated": True, "prices": []},
					]
				},
			),
		}
	)
	p = hetzner.HetznerProvider("tok", t)
	assert p.list_regions() == [{"code": "fsn1", "title": "Falkenstein (fsn1)", "country": "DE", "available": True}]
	(size,) = p.list_sizes()
	assert size["code"] == "cx32" and size["vcpu"] == 4 and size["memory_gb"] == 8.0 and size["disk_gb"] == 80
	assert size["monthly_cost"] == 6.8 and size["regions"] == ["fsn1", "hel1"]


def test_hetzner_ipv6_address():
	assert hetzner.ipv6_address("2a01:4f8:1c1c:abcd::/64") == "2a01:4f8:1c1c:abcd::1"
	assert hetzner.ipv6_address(None) is None


def test_digitalocean_create_server_request():
	t = FakeTransport({("POST", "/droplets"): (202, {"droplet": {"id": 99, "status": "new", "networks": {"v4": [], "v6": []}}})})
	p = base.get_provider("DigitalOcean", "do-token", t)
	result = p.create_server("erp-1", "fra1", "s-2vcpu-4gb", "ubuntu-24-04-x64", "#cloud-config\n", ["42", "aa:bb"], {"nexaploy-server": "srv-1"})
	assert result == {"id": "99", "ipv4": None, "ipv6": None, "status": "new"}
	(call,) = t.calls
	assert call["url"] == "https://api.digitalocean.com/v2/droplets"
	assert call["headers"]["Authorization"] == "Bearer do-token"
	body = call["body"]
	assert body["region"] == "fra1" and body["size"] == "s-2vcpu-4gb" and body["image"] == "ubuntu-24-04-x64"
	assert body["ssh_keys"] == [42, "aa:bb"] and body["ipv6"] is True
	assert body["tags"] == ["nexaploy", "nexaploy-server:srv-1"]


def test_digitalocean_get_delete_catalog():
	t = FakeTransport(
		{
			("GET", "/droplets/99"): (
				200,
				{"droplet": {"id": 99, "status": "active", "networks": {
					"v4": [{"ip_address": "10.0.0.2", "type": "private"}, {"ip_address": "9.9.9.9", "type": "public"}],
					"v6": [{"ip_address": "2a03::1", "type": "public"}]}}},
			),
			("GET", "/droplets/100"): (404, {"id": "not_found", "message": "The resource you were accessing could not be found."}),
			("DELETE", "/droplets/99"): (204, None),
			("GET", "/regions"): (200, {"regions": [{"slug": "fra1", "name": "Frankfurt 1", "available": True}]}),
			("GET", "/sizes"): (200, {"sizes": [
				{"slug": "s-2vcpu-4gb", "vcpus": 2, "memory": 4096, "disk": 80, "price_monthly": 24.0, "available": True, "regions": ["fra1"]},
				{"slug": "gone", "available": False},
			]}),
		}
	)
	p = digitalocean.DigitalOceanProvider("tok", t)
	assert p.get_server("99") == {"id": "99", "ipv4": "9.9.9.9", "ipv6": "2a03::1", "status": "active"}
	assert p.get_server("100") is None
	assert p.delete_server("99") is True
	assert p.list_regions() == [{"code": "fra1", "title": "Frankfurt 1", "country": None, "available": True}]
	assert p.list_sizes() == [{"code": "s-2vcpu-4gb", "vcpu": 2, "memory_gb": 4.0, "disk_gb": 80, "monthly_cost": 24.0, "regions": ["fra1"]}]


def test_provider_error_message_from_body():
	t = FakeTransport({("GET", "/droplets/1"): (401, {"id": "unauthorized", "message": "Unable to authenticate you"})})
	with pytest.raises(base.ProviderError) as exc:
		digitalocean.DigitalOceanProvider("bad", t).get_server("1")
	assert exc.value.status == 401 and "Unable to authenticate you" in str(exc.value)


# --------------------------------------------------------------------------- provisioning rules


def test_server_hostname_and_labels():
	assert pu.server_hostname("My ERP Server!", "srv-1a2b3c4d") == "my-erp-server"
	assert pu.server_hostname("", "srv-1a2b3c4d") == "srv-1a2b3c4d"
	assert pu.server_hostname("---", "srv-1a2b3c4d") == "srv-1a2b3c4d"
	assert len(pu.server_hostname("x" * 100, "srv-1")) == 63
	labels = pu.provider_labels("srv-1a2b3c4d", "acme team/1")
	assert labels == {"nexaploy": "1", "nexaploy-server": "srv-1a2b3c4d", "nexaploy-team": "acme-team-1"}
	assert pu.cloud_region_name("Hetzner Cloud", "fsn1") == "Hetzner Cloud-fsn1"


def test_billing_and_heartbeat_rules():
	assert pu.is_billable(None) and pu.is_billable("Not Provisioned")
	assert pu.is_billable("Bootstrapping") and pu.is_billable("Ready")
	for status in ("Creating", "Failed", "Deleting", "Deleted"):
		assert not pu.is_billable(status)
	assert pu.ready_on_heartbeat("Bootstrapping") and pu.ready_on_heartbeat("Failed")
	assert not pu.ready_on_heartbeat("Ready") and not pu.ready_on_heartbeat("Not Provisioned")
	assert not pu.ready_on_heartbeat("Deleting")


def test_bootstrap_timeout():
	now = datetime(2026, 11, 4, 12, 0)
	assert not pu.bootstrap_timed_out(now - timedelta(minutes=19), now)
	assert pu.bootstrap_timed_out(now - timedelta(minutes=21), now)
	assert pu.bootstrap_timed_out(None, now)


def test_registration_action():
	assert pu.registration_action(None, None) == "create"
	pre = {"name": "srv-1", "status": "Pending", "token_hash": None, "provisioning_status": "Bootstrapping"}
	assert pu.registration_action("srv-1", pre) == "attach"
	for broken in (
		None,
		{**pre, "status": "Archived"},
		{**pre, "provisioning_status": "Deleting"},
		{**pre, "token_hash": "abc"},
	):
		with pytest.raises(ValueError):
			pu.registration_action("srv-1", broken)


def test_attach_values():
	now = datetime(2026, 11, 4, 12, 0)
	server = {"provider": "Hetzner Cloud", "provisioning_status": "Bootstrapping"}
	values = pu.attach_values(server, "erp-1", "0.4.0", "{}", "h" * 64, now)
	assert values == {
		"status": "Active", "token_hash": "h" * 64, "agent_version": "0.4.0", "facts": "{}",
		"last_heartbeat": now, "hostname": "erp-1", "provisioning_status": "Ready",
	}
	# a manually pre-created Hosted server (no provider) keeps Not Provisioned
	manual = pu.attach_values({"provider": None, "provisioning_status": "Not Provisioned"}, None, None, "{}", "x", now)
	assert "provisioning_status" not in manual and "hostname" not in manual


def test_deletion_blockers():
	server = {"ownership": "Hosted", "provider": "Hetzner Cloud", "status": "Active", "provisioning_status": "Ready"}
	assert pu.deletion_blockers(server, 0, 0) == []
	assert pu.deletion_blockers({**server, "provisioning_status": "Failed"}, 0, 0) == []
	assert pu.deletion_blockers(server, 2, 0) == ["Delete or move the sites on this server first"]
	assert pu.deletion_blockers(server, 0, 1) == ["Archive the benches on this server first"]
	assert pu.deletion_blockers({**server, "ownership": "BYOS"}, 0, 0)
	assert pu.deletion_blockers({**server, "provider": None}, 0, 0)
	assert pu.deletion_blockers({**server, "provisioning_status": "Deleting"}, 0, 0)


def test_build_options():
	providers = [{"name": "Hetzner Cloud", "provider": "Hetzner", "title": "Hetzner Cloud"}, {"name": "DO", "provider": "DigitalOcean"}]
	regions = [
		{"name": "Hetzner Cloud-nbg1", "provider": "Hetzner Cloud", "code": "nbg1", "title": "Nuremberg"},
		{"name": "Hetzner Cloud-fsn1", "provider": "Hetzner Cloud", "code": "fsn1", "title": "Falkenstein"},
		{"name": "DO-fra1", "provider": "DO", "code": "fra1", "title": "Frankfurt"},
	]
	sizes = [
		{"name": "Hetzner Cloud-cx42", "provider": "Hetzner Cloud", "code": "cx42", "vcpu": 8, "memory_gb": 16, "disk_gb": 160, "plan": "Hosted Large"},
		{"name": "Hetzner Cloud-cx32", "provider": "Hetzner Cloud", "code": "cx32", "vcpu": 4, "memory_gb": 8, "disk_gb": 80, "plan": "Hosted Small"},
		{"name": "Hetzner Cloud-cx22", "provider": "Hetzner Cloud", "code": "cx22", "plan": "Disabled"},
		{"name": "DO-s", "provider": "DO", "code": "s-2vcpu-4gb", "plan": "Site Basic"},  # wrong plan type
	]
	plans = {
		"Hosted Small": {"title": "Hosted Small", "price_monthly": 40, "currency": "USD", "plan_type": "Server Hosted", "enabled": 1},
		"Hosted Large": {"title": "Hosted Large", "price_monthly": "80.5", "currency": "USD", "plan_type": "Server Hosted", "enabled": 1},
		"Disabled": {"title": "Old", "price_monthly": 10, "plan_type": "Server Hosted", "enabled": 0},
		"Site Basic": {"title": "Site Basic", "price_monthly": 5, "plan_type": "Site", "enabled": 1},
	}
	(opt,) = pu.build_options(providers, regions, sizes, plans)  # DO has no sellable size
	assert opt["name"] == "Hetzner Cloud"
	assert [r["code"] for r in opt["regions"]] == ["fsn1", "nbg1"]
	assert [(s["code"], s["price_monthly"]) for s in opt["sizes"]] == [("cx32", 40.0), ("cx42", 80.5)]
	assert opt["sizes"][0]["plan"] == "Hosted Small" and opt["sizes"][0]["currency"] == "USD"


def test_register_attaches_to_pre_created_server():
	"""api/agent.register: a token bound to a Server activates it instead of creating one."""
	src = read("api", "agent.py")
	register = src[src.index("def register("): src.index("def _attach_registration(")]
	assert '"server"]' in register or '"server"],' in register
	assert register.index("if row.server:") < register.index('"doctype": "Server"')
	attach = src[src.index("def _attach_registration("): src.index("# ---", src.index("def _attach_registration("))]
	assert "registration_action(" in attach and "attach_values(" in attach
	assert "on_server_activated(" in attach and '"used": 1' in attach
	assert "insert(" not in attach
	heartbeat = src[src.index("def heartbeat("): src.index("def poll(")]
	assert "mark_ready(" in heartbeat


def test_jobs_bind_token_to_server_and_use_cloud_init():
	src = read("provisioning", "jobs.py")
	token_fn = src[src.index("def create_registration_token("): src.index("# ---", src.index("def create_registration_token("))]
	assert '"server": server' in token_fn and "hash_token(token)" in token_fn
	assert "build_cloud_init(" in src and "frappe.enqueue(" in src
	assert "BOOTSTRAP_TIMEOUT_MINUTES" in src


# --------------------------------------------------------------------------- SaaS signup


def test_normalize_inputs():
	assert su.normalize_slug(" ERPNext ") == "erpnext"
	with pytest.raises(ValueError):
		su.normalize_slug("../etc")
	assert su.normalize_email(" Jane@Example.COM ") == "jane@example.com"
	for bad in ("", "jane", "jane@", "jane@example", "a b@example.com"):
		with pytest.raises(ValueError):
			su.normalize_email(bad)
	assert su.check_password("secret123") == "secret123"
	for bad in ("short1", "lettersonly", "12345678", " secret123", "x1" * 70):
		with pytest.raises(ValueError):
			su.check_password(bad)
	assert su.clean_text("  Acme \n Corp ") == "Acme Corp"


def test_site_name_for_and_subdomain_status():
	assert su.site_name_for("Acme", "erp.example.com") == ("acme", "acme.erp.example.com")
	assert su.site_name_for("my shop", None) == ("my-shop", "my-shop.nexaploy.cloud")
	for bad in ("ab", "www", "admin", "a.b", "acme.nexaploy.cloud"):
		with pytest.raises(ValueError):
			su.site_name_for(bad, "nexaploy.cloud")
	taken = {"taken.nexaploy.cloud"}
	ok = su.subdomain_status("Fresh", "nexaploy.cloud", taken.__contains__)
	assert ok == {"available": True, "subdomain": "fresh", "site_name": "fresh.nexaploy.cloud", "reason": None}
	no = su.subdomain_status("taken", "nexaploy.cloud", taken.__contains__)
	assert no["available"] is False and no["reason"] == "This subdomain is already taken"
	bad = su.subdomain_status("www", "nexaploy.cloud", taken.__contains__)
	assert bad["available"] is False and bad["site_name"] is None and bad["reason"]


def test_select_bench_least_sites():
	benches = [
		{"name": "erp-b", "status": "Active", "server_status": "Active"},
		{"name": "erp-a", "status": "Active", "server_status": "Active"},
		{"name": "erp-c", "status": "Active", "server_status": "Offline"},
		{"name": "erp-d", "status": "Pending", "server_status": "Active"},
	]
	assert su.select_bench(benches, {"erp-a": 5, "erp-b": 3, "erp-c": 0, "erp-d": 0}) == "erp-b"
	assert su.select_bench(benches, {"erp-a": 3, "erp-b": 3}) == "erp-a"  # tie -> name
	assert su.select_bench(benches, {}) == "erp-a"
	assert su.select_bench(benches[2:], {}) is None
	assert su.select_bench([], {}) is None
	with pytest.raises(ValueError):
		su.select_bench(benches, {}, "Random")


def test_trial_dates():
	start = date(2026, 11, 4)
	assert su.trial_end_date(start, 14) == date(2026, 11, 18)
	assert su.trial_end_date("2026-11-04", "7") == date(2026, 11, 11)
	assert su.trial_end_date(start, 0) is None and su.trial_end_date(start, None) is None
	# billing starts on the trial end, never in the past
	assert su.billing_start_date(start, date(2026, 11, 18)) == date(2026, 11, 18)
	assert su.billing_start_date(start, date(2026, 11, 1)) == start
	assert su.billing_start_date(start, None) == start
	assert su.in_trial("2026-11-18", start) and not su.in_trial("2026-11-04", start)


@pytest.mark.parametrize(
	"today,has_payment,state,site_status,expected",
	[
		("2026-11-17", False, "", "Active", None),  # still in trial
		("2026-11-18", False, "", "Active", "notify"),
		("2026-11-19", False, "Notified", "Active", None),  # grace period
		("2026-11-21", False, "Notified", "Active", "suspend"),
		("2026-11-21", False, "Notified", "Suspended", None),  # suspended by someone else
		("2026-11-25", False, "Suspended", "Suspended", None),
		("2026-11-18", True, "", "Active", "convert"),
		("2026-11-25", True, "Suspended", "Suspended", "unsuspend"),
		("2026-11-25", True, "Suspended", "Active", "convert"),
		("2026-12-25", False, "Converted", "Active", None),
	],
)
def test_trial_action(today, has_payment, state, site_status, expected):
	assert su.trial_action("2026-11-18", today, has_payment, state, site_status) == expected


def test_trial_action_without_trial_and_team_can_pay():
	assert su.trial_action(None, "2026-11-18", False, "") is None
	assert su.team_can_pay(0, 0) is False
	assert su.team_can_pay("12.5", 0) is True
	assert su.team_can_pay(None, 1) is True


def test_signup_status_and_verification_expiry():
	now = datetime(2026, 11, 4, 12, 0)
	assert su.verification_expired(None, now)
	assert su.verification_expired(now, now)
	assert not su.verification_expired(now + timedelta(hours=1), now)
	assert su.next_signup_status("Provisioning", "Pending", "Running") == "Provisioning"
	assert su.next_signup_status("Provisioning", "Active", "Success") == "Active"
	assert su.next_signup_status("Provisioning", "Broken", None) == "Failed"
	assert su.next_signup_status("Provisioning", "Pending", "Failure") == "Failed"
	assert su.next_signup_status("Pending Verification", "Active", None) == "Pending Verification"


def test_signup_api_is_guest_and_rate_limited():
	src = read("api", "signup.py")
	for fn in ("check_subdomain", "signup", "verify_email", "signup_status"):
		assert re.search(rf"@frappe\.whitelist\(allow_guest=True[^)]*\)\n@rate_limit\([^)]*\)\ndef {fn}\(", src), fn
	assert "from frappe.rate_limiter import rate_limit" in src
	assert "hash_token(" in src  # tokens stored as sha256 only


def test_subscriptions_start_after_trial():
	src = read("billing", "subscriptions.py")
	assert "billing_start_date(" in src and "start_date=start" in src
	assert '"start_date": start_date or today()' in src
	assert "is_billable(" in src


def test_signup_page_and_translations():
	assert os.path.isfile(os.path.join(APP, "www", "signup.html"))
	assert "get_context" in read("www", "signup.py")
	assert os.path.isfile(os.path.join(APP, "public", "js", "signup.js"))
	assert os.path.isfile(os.path.join(APP, "public", "js", "provisioning.js"))
	assert "/assets/nexaploy/js/provisioning.js" in read("www", "dashboard", "servers.html")
	string_re = re.compile(r"""(?<![A-Za-z0-9.])_{1,2}\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")
	strings = set()
	for parts in (("www", "signup.html"), ("templates", "signup_base.html"), ("www", "signup.py")):
		strings |= {m.group(1) or m.group(2) for m in string_re.finditer(read(*parts))}
	for js in ("signup.js", "provisioning.js"):
		strings |= set(re.findall(r'\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]', read("public", "js", js)))
	rows = []
	for path in [os.path.join(APP, "translations", "ar.csv")] + glob.glob(os.path.join(APP, "translations", "ar.*.part")):
		with open(path, encoding="utf-8", newline="") as fh:
			rows += [r for r in csv.reader(fh) if r]
	translated = {r[0] for r in rows}
	missing = sorted(strings - translated)
	assert not missing, missing
