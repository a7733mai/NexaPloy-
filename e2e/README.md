# End-to-end run on Docker

What was run for real (Docker 29, Frappe/ERPNext v15.89) and how to repeat it.
No apt/PyPI access is needed inside containers.

## 1. Bench image (offline)

```bash
e2e/build-test-image.sh mirror.gcr.io/frappe/erpnext:v15.89.0 nexaploy/bench:e2e-v15-20261104T0000
```

Adapts the official image to the Nexaploy image contract (UID 3113, baked assets,
`nexaploy-entrypoint.sh`, no `sites/assets` VOLUME). Production images come from `image-builder/`.

## 2. Server stack + agent (no systemd, custom root)

```bash
sudo bash agent/install/install.sh --root /srv/nexaploy-test --skip-packages --no-systemd \
  --skip-frappe-docker --acme-email ops@example.com --agent-source "$PWD"
export NEXAPLOY_CONFIG=/srv/nexaploy-test/config/agent.toml
/srv/nexaploy-test/agent/.venv/bin/nexaploy-agent api &
/srv/nexaploy-test/agent/.venv/bin/nexaploy-agent worker &
```

## 3. Bench + site through the agent CLI

```bash
nexaploy bench create erp-v15 --image nexaploy/bench:e2e-v15-20261104T0000 --wait
nexaploy site create erp1.example.com --bench erp-v15 --admin-password '...' -a erpnext --wait
curl -k --resolve erp1.example.com:443:127.0.0.1 https://erp1.example.com/api/method/ping
nexaploy site backup erp1.example.com --wait
nexaploy site restore erp1.example.com --backup-id <id> --wait
nexaploy site add-domain erp1.example.com erp.customer.test --wait
nexaploy retry <failed job id> --wait     # failed local jobs resume at the failed step
```

## 4. Control plane on a real Frappe v15

Run a container from the same image on the `nexaploy-db` network, put
`control-plane/` at `apps/nexaploy` (a `.pth` file is enough), add `nexaploy` to
`sites/apps.txt`, then `bench new-site cp.local` + `bench --site cp.local install-app nexaploy`
and `bench serve`. Register the agent with a token from
`generate_registration_token(team=...)`:

```bash
nexaploy-agent register --control-plane http://127.0.0.1:8100 --token <token> --config "$NEXAPLOY_CONFIG"
```

Verified flow: registration → heartbeat (server facts) → Bench created in the desk →
`bench.create` polled and run by the agent → Bench Active → Site created → `site.create`
(admin password resolved only at poll time) → Site Active and reachable over HTTPS →
backup from the control plane with per-file records → all portal pages and APIs as a
customer → a second customer is refused everywhere (portal API and `frappe.client`).

## Not covered here

S3 upload/download (needs rclone + an S3 endpoint), Let's Encrypt (needs public DNS),
payment gateways, GitHub App, cloud provisioning, image builds with `image-builder/`.

## Portal in a real browser

All 15 portal pages were opened in Chromium (Playwright) as a customer: no JS errors,
Alpine components initialise, English and Arabic (RTL). Screenshots: `docs/screenshots/`.
Alpine.js is vendored (`control-plane/nexaploy/public/js/vendor/`) so customer pages load
nothing from third-party CDNs.

## Frappe v16 (verified)

```bash
e2e/build-test-image.sh mirror.gcr.io/frappe/erpnext:v16 nexaploy/bench:e2e-v16-20261104T0000
```

Verified with Frappe 16.55.1 / ERPNext 16.53.0 (Python 3.14, Node 24) on the same server
as the v15 benches:

- bench + site created from the control plane, HTTPS, login, records, assets
- SQL (read-only), console, logs, backup on v16
- **upgrade v15 → v16** from the portal (`move_site(..., allow_upgrade=1)`): backup on the
  v15 bench → restore + migrate on the v16 bench; data kept, custom domain re-added
- without `allow_upgrade` the move is refused

What v16 changed for us (fixed):

- its MariaDB 11.8 client requires TLS → the shared DB is now `mariadb:11.8` (TLS out of the
  box); an existing 10.6 data dir upgraded in place with v15 sites still working
- `--no-mariadb-socket` is deprecated → the agent uses `--mariadb-user-host-login-scope=%`
  and falls back to the old flag only when `bench new-site --help` lacks the new one (v14)
- the official v16 image already bakes assets in `./assets` and runs frappe as `1000:0`
  (handled by `e2e/Containerfile.official`)

### Control plane on Frappe v16 (verified)

The `nexaploy` app installed and migrated on Frappe 16.55.1; all 18 scheduler jobs ran
cleanly; agent registration/heartbeat, `bench.create`, `site.create` and `site.backup` from
the v16 control plane; all 15 portal pages in Chromium without JS errors; cross-team
isolation (portal API and `frappe.client`) holds. New Release Groups default to v16.
