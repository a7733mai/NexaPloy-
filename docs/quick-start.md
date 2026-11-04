# Quick start

There are two ways to run Nexaploy:

- **All-in-one.** The control plane and your sites run on one server. This suits one
  company or a small agency.
- **Hosted control plane with BYOS servers.** One control plane manages many servers.
  Some are servers the platform creates on Hetzner Cloud or DigitalOcean. Others are
  servers your customers bring and register with a one-time token.

## Requirements (every server)

- A fresh **Ubuntu 22.04 or 24.04** server, x86_64 or arm64. Debian 12 works on a best-effort basis.
- At least **2 GB RAM** and **20 GB free disk**. Every Frappe bench uses about 1–2 GB of RAM,
  so plan 4 GB or more for real use.
- **Ports 80 and 443 free** and reachable from the internet. Let's Encrypt validates
  certificates over port 80.
- Root access, and outbound HTTPS to Docker Hub, GitHub and your control plane.

## Option A — all-in-one on one server (new) { #all-in-one }

!!! note "New"
    The `--with-control-plane` option is being added to `install.sh` now. If your copy of
    `install.sh` does not list it under `--help`, use option B and install the control plane
    on the same server.

1. Point a DNS **A record** for your control plane domain (for example `cloud.example.com`)
   to the server's public IP address.
2. Run the installer as root:

    ```bash
    curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
      | sudo bash -s -- --with-control-plane cloud.example.com --acme-email ops@example.com
    ```

3. Open `https://cloud.example.com`, log in, and continue with
   [First site](#first-site) below.

You can also start from the **Nexaploy server image** (Hetzner Cloud or DigitalOcean snapshot,
built from `packer/`). Put `NEXAPLOY_DOMAIN=cloud.example.com` (and optionally
`NEXAPLOY_ACME_EMAIL=...`) in the server's user-data, and the install finishes by itself on
first boot.

## Option B — hosted control plane + BYOS servers { #byos }

### 1. Install the control plane

The control plane is a Frappe app (Frappe v15 or v16). Install it on a bench:

```bash
git clone https://github.com/a7733mai/NexaPloy- /srv/nexaploy
cd ~/frappe-bench
bench get-app /srv/nexaploy/control-plane
bench --site cp.example.com install-app nexaploy
bench --site cp.example.com migrate
bench --site cp.example.com enable-scheduler
```

Each release also publishes a control plane image, `ghcr.io/a7733mai/nexaploy-control-plane:<version>`.
The `:16` and `:latest` tags follow the newest release on Frappe v16.

Installing the app creates the **Nexaploy Customer** role and three default plans:
BYOS server $10/month, hosted server $40/month, site $5/month.

### 2. Publish `install.sh`

Servers download the installer from `https://<control-plane>/install.sh`. Serve
`agent/install/install.sh` at that path from the web server in front of Frappe. For example,
with nginx:

```nginx
location = /install.sh { alias /srv/nexaploy/agent/install/install.sh; default_type text/x-shellscript; }
```

Every GitHub Release also has `install.sh` attached.

### 3. Configure the platform

In the desk, open **Nexaploy Settings**:

- **Root domain**: new sites are named `<name>.<root domain>`. Point a wildcard DNS record
  (`*.<root domain>`) to your hosted servers.
- **Backup storage** (S3-compatible endpoint, region, bucket, keys). When you save,
  every server gets the storage settings (job `server.configure_backups`). Without this,
  backups stay on the server.
- **Registry** (optional, e.g. `ghcr.io/your-org`): a bench image built on one server can
  then be pulled by the others.
- **Alert e-mail** for operations alerts.

For hosted servers, add a **Cloud Provider** (Hetzner Cloud or DigitalOcean API token), sync
its regions and sizes, and enable the ones you sell.

### 4. Add a server

=== "BYOS (customer's server)"

    In the dashboard, open **Servers → Add your own server → Generate install command**.
    Run the command it shows, as root, on the new server:

    ```bash
    curl -fsSL https://cp.example.com/install.sh | sudo bash -s -- \
      --token <registration-token> --control-plane https://cp.example.com
    ```

    The token can be used once and is valid for 24 hours. The page shows the server as
    **Active** when its first heartbeat arrives.

=== "Hosted (created by the platform)"

    In the dashboard, open **Servers → Create hosted server**, then choose a provider, a region
    and a size. Nexaploy creates the VM, and cloud-init runs `install.sh` with a registration
    token. This usually takes 5–10 minutes.

=== "Server image"

    Create a server from the Nexaploy snapshot and set its user-data to:

    ```
    NEXAPLOY_CONTROL_PLANE=https://cp.example.com
    NEXAPLOY_TOKEN=<registration-token>
    ```

## First site { #first-site }

1. **Bench Groups → New Bench Group.** Pick a Frappe version (v16 is the default) and apps
   from the marketplace or your own GitHub apps. On the group page, click **Deploy now**. The
   build server then builds one Docker image (job `image.build`). The first build takes a while.
2. On the same page, choose a server under **Benches on your servers** and click
   **Deploy to server** (job `bench.create`).
3. **Sites → New Site.** Choose where to deploy, enter a site name, pick apps, then click
   **Create site**. Save the administrator password: it is shown only once.
4. The site is served over HTTPS as soon as Let's Encrypt issues its certificate. To use
   your own domain, point its DNS to the server and add the domain on the site page.

### Without a control plane (standalone)

The agent also works alone, from the command line:

```bash
curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
  | sudo bash -s -- --acme-email ops@example.com
nexaploy build --tag nexaploy/bench:erp-v16-1 --major 16 --apps-json apps.json --wait
nexaploy bench create erp-v16 --image nexaploy/bench:erp-v16-1 --wait
nexaploy site create erp.example.com --bench erp-v16 -a erpnext --wait   # asks for the admin password
```
