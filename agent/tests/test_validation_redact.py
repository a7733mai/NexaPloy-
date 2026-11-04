import pytest

from nexaploy_agent import validation as v
from nexaploy_agent.redact import MASK, collect_secrets, mask_text, redact_params
from nexaploy_agent.ulid import is_ulid, new_ulid


@pytest.mark.parametrize("name", ["erp-v15-main", "abc", "a12", "x" + "y" * 30])
def test_bench_ok(name):
    assert v.bench_name(name) == name


@pytest.mark.parametrize("name", ["ab", "1abc", "Abc", "erp_v15", "a" * 32, "erp v15", "../etc", "", None, "-ab"])
def test_bench_bad(name):
    with pytest.raises(v.ValidationError):
        v.bench_name(name)


@pytest.mark.parametrize("name", ["erp.example.com", "a.io", "x-1.y-2.example.co.uk", "abc.qa"])
def test_site_ok(name):
    assert v.site_name(name) == name


@pytest.mark.parametrize("name", ["localhost", "Erp.example.com", "-a.example.com", "a-.example.com",
                                  "a..com", "a.c", "a.example.com/x", "a.example.com;rm", "--help.com",
                                  "a" * 64 + ".com"])
def test_site_bad(name):
    with pytest.raises(v.ValidationError):
        v.site_name(name)


def test_slug():
    assert v.slug("erp.example.com") == "erp-example-com"


@pytest.mark.parametrize("ref", [
    "nexaploy/bench:erp-v15-main-20261103T2130", "registry.example.com:5000/nexaploy/bench:v1", "redis:7-alpine",
])
def test_image_ok(ref):
    assert v.image_ref(ref) == ref


@pytest.mark.parametrize("ref", ["nexaploy/bench", "--privileged", "Nexaploy/bench:1", "a:b c", "x:-tag"])
def test_image_bad(ref):
    with pytest.raises(v.ValidationError):
        v.image_ref(ref)


def test_app_config_backup_remote():
    assert v.app_name("erpnext") == "erpnext"
    with pytest.raises(v.ValidationError):
        v.app_name("--yes")
    assert v.config_key("maintenance_mode") == "maintenance_mode"
    with pytest.raises(v.ValidationError):
        v.config_key("db_password")
    with pytest.raises(v.ValidationError):
        v.config_key("a b")
    assert v.backup_id("20261103T213000Z")
    with pytest.raises(v.ValidationError):
        v.backup_id("../x")
    assert v.remote_path("srv/erp/a.com/20261103T213000Z/") == "srv/erp/a.com/20261103T213000Z"
    for bad in ("../x", "a/../b", "/abs", "a b"):
        with pytest.raises(v.ValidationError):
            v.remote_path(bad)


def test_redact_params():
    params = {
        "name": "a.example.com", "admin_password": "hunter22", "apps": ["erpnext"],
        "nested": {"api_token": "t0k", "Secret_Value": "s", "fine": 1},
        "list": [{"db_root_password": "p"}],
    }
    out = redact_params(params)
    assert out["admin_password"] == MASK
    assert out["nested"] == {"api_token": MASK, "Secret_Value": MASK, "fine": 1}
    assert out["list"][0]["db_root_password"] == MASK
    assert out["name"] == "a.example.com" and out["apps"] == ["erpnext"]
    assert params["admin_password"] == "hunter22"  # input untouched


def test_redact_set_config_value_when_key_sensitive():
    assert redact_params({"site": "a.io", "key": "api_secret", "value": "zzz"})["value"] == MASK
    plain = redact_params({"site": "a.io", "key": "maintenance_mode", "value": 1})
    assert plain["value"] == 1 and plain["key"] == MASK  # literal §5.3 rule: "key" matches


def test_mask_text():
    secrets = collect_secrets({"admin_password": "hunter22", "x": {"token": "abcd1234"}})
    assert secrets == {"hunter22", "abcd1234"}
    text = "$ bench new-site --admin-password hunter22 https://abcd1234@github.com/x https://u:p@h.io/y"
    masked = mask_text(text, secrets)
    assert "hunter22" not in masked and "abcd1234" not in masked and "u:p@" not in masked


def test_ulid_sortable_unique():
    ids = [new_ulid() for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert ids == sorted(ids)
    assert all(is_ulid(i) for i in ids)
    assert new_ulid(1000) < new_ulid(2000)
