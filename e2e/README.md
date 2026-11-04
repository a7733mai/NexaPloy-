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
