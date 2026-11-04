"""Auto-provisioning of Hosted servers on cloud providers (Hetzner Cloud, DigitalOcean).

Pure modules (no frappe import, unit-tested in tests/test_provisioning.py):
    base.py           Provider interface, HTTP transport indirection, get_provider()
    hetzner.py        Hetzner Cloud API v1
    digitalocean.py   DigitalOcean API v2
    cloud_init.py     #cloud-config that runs install.sh with a registration token
    utils.py          naming, labels, state rules, register-attach decision, options

Frappe glue: jobs.py (background provisioning, check_provisioning task) and
nexaploy/api/provisioning.py (portal API).
"""
