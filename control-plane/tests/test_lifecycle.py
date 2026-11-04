"""Site lifecycle (ARCHITECTURE.md §10): pure helpers in nexaploy.lifecycle_utils + static checks."""

import ast
import csv
import json
import os
import re
from datetime import datetime, timedelta

import pytest

from nexaploy import lifecycle_utils as lu

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "nexaploy")


# --------------------------------------------------------------------------- staging


def test_staging_site_name():
	assert lu.staging_site_name("erp.example.com") == "staging-erp.example.com"
	assert lu.staging_site_name("Shop.Nexaploy.Cloud") == "staging-shop.nexaploy.cloud"
	with pytest.raises(ValueError):
		lu.staging_site_name("x" * 60 + ".example.com")  # first label would exceed 63 chars
	with pytest.raises(ValueError):
		lu.staging_site_name("not a site")
	assert lu.clone_params("erp-v15", "a.example.com", "staging-a.example.com") == {
		"bench": "erp-v15", "site": "a.example.com", "target_site": "staging-a.example.com", "scrub": True,
	}


# --------------------------------------------------------------------------- move


def test_move_compatibility():
	assert lu.move_compatibility("15", 15) == {"ok": True, "upgrade": False, "reason": None}
	r = lu.move_compatibility(14, "15")
	assert not r["ok"] and r["upgrade"] and "allow_upgrade" in r["reason"]
	assert lu.move_compatibility(14, 15, allow_upgrade=True) == {"ok": True, "upgrade": True, "reason": None}
	r = lu.move_compatibility(16, 15, allow_upgrade=True)
	assert not r["ok"] and "downgrade" in r["reason"]
	assert not lu.move_compatibility(None, 15)["ok"]


def test_plan_move_and_restore_params():
	assert lu.plan_move(True) == ["site.backup", "site.drop", "site.restore"]
	assert lu.plan_move(False) == ["site.backup", "site.restore", "site.drop"]
	for same in (True, False):
		steps = lu.plan_move(same)
		assert steps[0] == "site.backup" and "site.create" not in steps  # restore creates the site
	# a failure before the drop leaves the source running -> abort
	cross = lu.plan_move(False)
	assert not lu.move_source_dropped(cross, 1)  # restore failed
	same = lu.plan_move(True)
	assert lu.move_source_dropped(same, 2)  # restore failed after the drop -> Broken
	assert not lu.move_source_dropped(same, 0)
	# same server restores the local backup; another server needs the bucket copy
	assert lu.move_restore_params("a.example.com", "erp-v15", True, "20261103T213000Z", "r:x") == {
		"site": "a.example.com", "bench": "erp-v15", "with_files": True, "backup_id": "20261103T213000Z",
	}
	p = lu.move_restore_params("a.example.com", "erp-v16", False, "20261103T213000Z", "nexaploy-backups:p/a")
	assert p["remote_path"] == "nexaploy-backups:p/a" and "backup_id" not in p and p["bench"] == "erp-v16"
	with pytest.raises(ValueError, match="bucket"):
		lu.move_restore_params("a.example.com", "erp-v16", False, "20261103T213000Z", None)
	assert lu.move_restore_params("a.example.com", "erp-v15", True, None, "r:x")["remote_path"] == "r:x"


def test_dns_hint():
	h = lu.dns_hint("shop.nexaploy.cloud", ["www.shop.com"], "nexaploy.cloud", "srv2.example.net", "203.0.113.9")
	assert h["managed_hosts"] == ["shop.nexaploy.cloud"] and h["custom_hosts"] == ["www.shop.com"]
	assert h["record"] == "A" and h["target"] == "203.0.113.9" and "203.0.113.9" in h["message"]
	h = lu.dns_hint("erp.example.com", [], "nexaploy.cloud", "srv2.example.net")
	assert h["record"] == "CNAME" and h["custom_hosts"] == ["erp.example.com"] and not h["managed_hosts"]


# --------------------------------------------------------------------------- transfer


def test_transfer_tokens():
	token = lu.generate_transfer_token()
	assert len(token) >= 40 and ":" not in token
	digest = lu.hash_transfer_token(token)
	assert re.fullmatch(r"[0-9a-f]{64}", digest) and digest != token
	assert lu.verify_transfer_token(token, digest)
	assert not lu.verify_transfer_token("other", digest)
	assert not lu.verify_transfer_token(None, digest) and not lu.verify_transfer_token(token, None)
	now = datetime(2026, 11, 3, 12, 0)
	exp = lu.transfer_expiry(now)
	assert exp == now + timedelta(days=7)
	assert not lu.transfer_expired(exp, now) and lu.transfer_expired(exp, exp) and lu.transfer_expired(None, now)
	assert lu.transfer_url("https://cp.example.com/", "a/b") == "https://cp.example.com/dashboard/transfer?token=a%2Fb"


def test_parse_transfer_target():
	assert lu.parse_transfer_target(" Owner@Example.com ") == ("email", "owner@example.com")
	assert lu.parse_transfer_target("Acme-Corp") == ("team", "acme-corp")
	for bad in ("", "a@b", "bad team!", None):
		with pytest.raises(ValueError):
			lu.parse_transfer_target(bad)


def test_transfer_block_reason():
	assert lu.transfer_block_reason(None, None, "Hosted") is None  # shared hosted bench
	assert "Move it" in lu.transfer_block_reason(None, "team-a", "BYOS")  # own BYOS server
	assert lu.transfer_block_reason("team-a", None, "Hosted")  # team's own bench
	assert lu.transfer_block_reason(None, None, "BYOS")  # BYOS without a team: not shared hosting
	assert lu.is_shared_hosted("", "", "Hosted") and not lu.is_shared_hosted("", "", "BYOS")


# --------------------------------------------------------------------------- rename


def test_validate_rename():
	assert lu.validate_rename("a.example.com", " New.Example.com. ") == "new.example.com"
	with pytest.raises(ValueError, match="current name"):
		lu.validate_rename("a.example.com", "a.example.com")
	with pytest.raises(ValueError, match="taken"):
		lu.validate_rename("a.example.com", "b.example.com", taken={"b.example.com"})
	with pytest.raises(ValueError, match="FQDN"):
		lu.validate_rename("a.example.com", "no_dots")
	with pytest.raises(ValueError, match="reserved"):
		lu.validate_rename("a.example.com", "staging-a.example.com")
	assert lu.renamed_domains(["www.x.com", "new.x.com"], "old.x.com", "new.x.com") == ["www.x.com", "old.x.com"]
	assert lu.renamed_domains(["www.x.com"], "old.x.com", "new.x.com", keep_old=False) == ["www.x.com"]
	assert lu.rename_params("erp-v15", "old.x.com", "new.x.com") == {
		"bench": "erp-v15", "site": "old.x.com", "new_name": "new.x.com", "keep_old_domain": True,
	}
	assert lu.failed_step_index([{"step_idx": 0, "status": "success"}, {"step_idx": 1, "status": "failure"}]) == 1
	assert lu.failed_step_index([{"idx": 0, "status": "Failure"}]) == 0
	assert lu.failed_step_index([{"step_idx": 0, "status": "success"}]) is None


# --------------------------------------------------------------------------- static checks


def test_lifecycle_utils_is_pure():
	src = open(os.path.join(APP, "lifecycle_utils.py")).read()
	assert not re.search(r"^\s*(import|from)\s+frappe", src, re.M)


def test_api_whitelisted_and_logged():
	src = open(os.path.join(APP, "api", "site_lifecycle.py")).read()
	tree = ast.parse(src)
	funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
	for name in ("get_lifecycle", "create_staging", "refresh_staging", "move_site", "request_transfer",
	             "cancel_transfer", "get_transfer", "accept_transfer", "reject_transfer", "rename_site"):
		assert name in funcs, name
		assert any("whitelist" in ast.unparse(d) for d in funcs[name].decorator_list), name
	for name in ("create_staging", "refresh_staging", "move_site", "request_transfer", "cancel_transfer",
	             "accept_transfer", "reject_transfer", "rename_site"):
		assert "_log(" in ast.unparse(funcs[name]), name
	# log_activity is imported lazily and guarded
	assert "from nexaploy.activity import log_activity" in src and "except ImportError" in src


def test_site_doctype_lifecycle_fields():
	d = json.load(open(os.path.join(APP, "nexaploy", "doctype", "site", "site.json")))
	fields = {f["fieldname"]: f for f in d["fields"]}
	assert fields["is_staging"]["fieldtype"] == "Check"
	assert fields["staging_of"]["options"] == "Site" and fields["staging_site"]["options"] == "Site"
	assert fields["staging_site"].get("read_only") == 1
	assert "Renaming" in fields["status"]["options"].split("\n")
	assert set(d["field_order"]) == set(fields)


def test_site_transfer_doctype():
	d = json.load(open(os.path.join(APP, "nexaploy", "doctype", "site_transfer", "site_transfer.json")))
	fields = {f["fieldname"]: f for f in d["fields"]}
	for name in ("site", "from_team", "to_team", "to_email", "status", "token_hash", "expires_on"):
		assert name in fields, name
	assert fields["status"]["options"].split("\n") == list(lu.TRANSFER_STATUSES)
	assert fields["token_hash"].get("unique") == 1 and fields["token_hash"].get("hidden") == 1
	assert fields["from_team"]["options"] == "Team" and fields["to_team"]["options"] == "Team"


def test_job_kinds_and_callbacks():
	src = open(os.path.join(APP, "agent_job.py")).read()
	assert '"site.clone"' in src and '"site.rename"' in src
	cb = open(os.path.join(APP, "agent_callbacks.py")).read()
	assert '@handles("site.clone")' in cb and '@handles("site.rename")' in cb


def test_translations_part():
	path = os.path.join(APP, "translations", "ar.csv")
	with open(path, newline="") as fh:
		rows = list(csv.reader(fh))
	assert rows and all(len(r) == 2 and r[0] and r[1] for r in rows)
	assert len({r[0] for r in rows}) == len(rows)


def test_portal_pages():
	html = open(os.path.join(APP, "www", "dashboard", "site.html")).read()
	assert 'x-data="nxSiteLifecycle"' in html
	assert os.path.isfile(os.path.join(APP, "www", "dashboard", "transfer.html"))
	assert os.path.isfile(os.path.join(APP, "www", "dashboard", "transfer.py"))
	js = open(os.path.join(APP, "public", "js", "site_tools.js")).read()
	for comp in ("nxSiteTools", "nxSiteLifecycle", "nxTransfer"):
		assert f'Alpine.data("{comp}"' in js, comp
