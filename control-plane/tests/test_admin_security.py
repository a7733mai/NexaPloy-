"""Admin two-factor authentication: pure status logic + API/page wiring."""

import os
import subprocess

import pytest

from nexaploy import admin_security_utils as su

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nexaploy")
ADMINS = [{"user": "Administrator", "otp_configured": True}, {"user": "ops@x.test", "otp_configured": False}]


def test_not_enforced_unless_global_switch_and_role():
	assert su.summarize(0, "OTP App", 1, ADMINS)["level"] == "danger"
	assert su.summarize(1, "OTP App", 0, ADMINS)["enforced"] is False


def test_pending_setup_and_success():
	s = su.summarize(1, "OTP App", 1, ADMINS)
	assert s["enforced"] and s["pending_setup"] == ["ops@x.test"] and s["level"] == "warning"
	assert su.summarize(1, "Email", 1, ADMINS)["level"] == "success"  # e-mail codes need no setup


def test_validate_method():
	assert su.validate_method(None) == "OTP App"
	with pytest.raises(ValueError):
		su.validate_method("Carrier pigeon")


def test_api_is_admin_only_and_logged():
	src = open(os.path.join(APP, "api", "admin_security.py")).read()
	for fn in ("def get_two_factor_status", "def require_admin_two_factor"):
		body = src.split(fn, 1)[1].split("\ndef ", 1)[0]
		assert "_admin()" in body
	assert 'methods=["POST"]' in src and "platform.admin_two_factor" in src and "allow_guest" not in src
	assert "import frappe" not in open(os.path.join(APP, "admin_security_utils.py")).read()


def test_admin_page_wiring():
	html = open(os.path.join(APP, "www", "dashboard", "admin.html")).read()
	assert "/assets/nexaploy/js/admin_security.js" in html and "templates/includes/admin_security.html" in html
	part = open(os.path.join(APP, "templates", "includes", "admin_security.html")).read()
	assert part.count("<template") == part.count("</template>")
	subprocess.run(["node", "--check", os.path.join(APP, "public", "js", "admin_security.js")], check=True)


def test_enable_blocked_without_a_delivery_channel():
	assert "outgoing e-mail" in su.enable_blocker("OTP App", False, False)
	assert "outgoing e-mail" in su.enable_blocker("Email", False, True)
	assert "SMS" in su.enable_blocker("SMS", True, False)
	assert su.enable_blocker("OTP App", True, False) is None
