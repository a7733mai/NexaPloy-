# Operator guide

This guide is for the **operator**: the person or company that runs the Nexaploy platform
(the "mother panel") and logs in as `Administrator` or as another user with the
**System Manager** role. Customers use the [customer guide](customer.md).

As an operator you install the control plane, connect servers, set prices, storage, e-mail and
branding, review marketplace apps, roll out agent releases and keep everything healthy.

!!! note "Two places to work"
    - The **dashboard** at `https://<control-plane>/dashboard`. Platform admins see an extra
      sidebar entry **Administration** (`/dashboard/admin`) and **Admin desk**.
    - The **Frappe desk** at `https://<control-plane>/app`. A few things only exist in the desk:
      *Nexaploy Settings* (root domain), *Cloud Provider*, *GitHub Settings*, *Uptime Probe*,
      *SaaS Product*, *Support Ticket* replies, *Publisher Payout* and *Agent Job* retries.

## 1. Choose an installation model

| Model | What runs where | Good for |
|---|---|---|
| **All-in-one** | Control plane and sites on **one** server (`install.sh --with-control-plane`) | One company, a small agency, a trial |
| **Hosted control plane + servers** | Control plane on its own Frappe bench; many servers (Hosted on Hetzner/DigitalOcean, or customers' own BYOS servers) join it with a token | A hosting business with many customers |

The two models share the same code. Trade-off of all-in-one: when that server is down, the
control plane (and its alerts) is down too. Keep backup storage **off the server** in that case.

### Requirements for every server

| Item | Requirement |
|---|---|
| OS | Ubuntu 22.04 or 24.04 (Debian 12 best effort), x86_64 or aarch64 |
| RAM | At least 2 GB (plan 4 GB or more; each bench uses about 1–2 GB) |
| Disk | At least 20 GB free under `/opt/nexaploy` |
| Inbound ports | **80 and 443** only, open to the internet (Let's Encrypt validates over port 80) |
| Outbound | HTTPS to Docker Hub/GHCR, GitHub, rclone.org and your control plane |
| Access | root (sudo). The platform never needs SSH to the server. |

## 2. Install all-in-one

1. Create a DNS **A record** (and AAAA if the server has IPv6) for the control plane domain,
   e.g. `cloud.example.com`, pointing to the server.
2. Run the installer as root:

    ```bash
    curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
      | sudo bash -s -- --with-control-plane cloud.example.com --acme-email ops@example.com
    ```

    Without options, and with a terminal, the installer asks instead:

    ```
    Nexaploy setup
      1) New: install the control plane on this server (all-in-one)
      2) Join: connect this server to an existing control plane
      3) Standalone agent only (CLI, no control plane)
    ```

    Option 1 asks for the domain and the Let's Encrypt e-mail.

    Before it starts, the installer checks that the control plane domain resolves to this
    server (DNS preflight), because the certificate cannot be issued otherwise. Skip the check
    with `--skip-dns-check` (e.g. behind a load balancer). The control plane image is chosen
    automatically; if it cannot be pulled, the installer builds it locally.
3. At the end the installer prints:

    ```
    ==> Control plane: https://cloud.example.com
      login:    Administrator
      password: stored in /opt/nexaploy/config/control-plane.env (ADMIN_PASSWORD)
    ```

4. Read the password and log in:

    ```bash
    sudo grep ADMIN_PASSWORD /opt/nexaploy/config/control-plane.env
    ```

    You can choose it yourself with `--admin-password`. The file has mode 600; change the
    password after the first login (**Account → Change password**) and keep the file safe.

What all-in-one does: it creates a bench `nexaploy-cp` from the image
`ghcr.io/a7733mai/nexaploy-control-plane:16` (override with `--control-plane-image`), publishes it
only on `127.0.0.1:8090` for the local agent (`--control-plane-port`), creates the site for your
domain with the `nexaploy` app, creates the operator team **Platform** (billing exempt) and
registers the server to its own control plane. The server is marked as the control plane host:
it cannot be archived, and the control plane can never drop or move itself. A daily task backs up
the control plane site (uploaded once backup storage is configured).

!!! tip "Server images"
    The Hetzner Cloud / DigitalOcean snapshots built from `packer/` finish the install on first
    boot. Put `NEXAPLOY_DOMAIN=cloud.example.com` (all-in-one) or
    `NEXAPLOY_CONTROL_PLANE=…` + `NEXAPLOY_TOKEN=…` (join) in the user-data.

## 3. Install a hosted control plane with separate servers

1. Install the Frappe app on a Frappe v15 or v16 bench:

    ```bash
    git clone https://github.com/a7733mai/NexaPloy- /srv/nexaploy
    cd ~/frappe-bench
    bench get-app /srv/nexaploy/control-plane
    bench --site cp.example.com install-app nexaploy
    bench --site cp.example.com migrate
    bench --site cp.example.com enable-scheduler
    ```

    The `Administrator` password is the one you gave to `bench new-site`.
2. Publish the installer at `https://<control-plane>/install.sh` (nginx example):

    ```nginx
    location = /install.sh { alias /srv/nexaploy/agent/install/install.sh; default_type text/x-shellscript; }
    ```

3. In the desk open **Nexaploy Settings** and set the **root domain** (new shared sites are
   `<name>.<root domain>`). Point a wildcard record `*.<root domain>` to your hosted servers.
4. Add servers (next section).

## 4. Add servers

=== "Customer or operator BYOS server"

    1. In the dashboard open **Servers → Add your own server → Generate install command**.
    2. Run the command it shows as root on a fresh Ubuntu server:

        ```bash
        curl -fsSL https://cp.example.com/install.sh | sudo bash -s -- \
          --token <registration-token> --control-plane https://cp.example.com
        ```

    3. The page shows *Waiting for your server to register…*, then
       *Your server is connected and active.* The token is single use and valid for 24 hours.

=== "Hosted server (created by the platform)"

    1. In the desk create a **Cloud Provider** (Hetzner or DigitalOcean, API token) and run
       **Sync catalog**. Regions and sizes are imported **disabled**.
    2. Enable the regions you sell and, for each **Cloud Server Size**, enable it and link a
       *Server Hosted* **Nexaploy Plan** (the price the customer pays). Sizes without a plan are
       not offered.
    3. Customers (or you, in the Platform team) use **Servers → Create hosted server**, choose
       *Provider*, *Region*, *Server size* and click **Create server**. The VM boots, runs
       `install.sh` through cloud-init and registers. It takes 5–10 minutes; after 20 minutes
       without registration it is marked *Failed* and you get an alert.

=== "Hosted server created by hand"

    Create a **Server** in the desk (ownership *Hosted*), call **Rotate token**, then install with
    `--server-id <id> --token <server token> --control-plane https://cp.example.com`.

### Every `install.sh` option

| Option | Meaning |
|---|---|
| `--control-plane URL` | Join this control plane (with `--token`). |
| `--token TOKEN` | With `--control-plane`: the one-time registration token. Alone: the local API token. |
| `--with-control-plane DOMAIN` | All-in-one: install the control plane on this server. |
| `--control-plane-image IMG` | Control plane image (default `ghcr.io/a7733mai/nexaploy-control-plane:16`). |
| `--control-plane-port PORT` | Local port the agent uses to reach it (default `8090`). |
| `--admin-password PASS` | Administrator password (default random, saved in `config/control-plane.env`). |
| `--skip-dns-check` | All-in-one: do not check that the control plane domain points to this server. |
| `--restore-control-plane BACKUP` | All-in-one: restore an existing control plane backup on this new server (moving the control plane). |
| `--server-id ID` | Fixed server id. |
| `--acme-email EMAIL` | Let's Encrypt account e-mail. Always set it. |
| `--agent-source SRC`, `--branch BRANCH` | Where the agent code comes from (use a release tag in production). |
| `--frappe-docker-ref REF` | `frappe_docker` ref for image builds. |
| `--force-register` | Register again although a token is stored. |
| `--skip-preflight` | Skip OS/arch/RAM/disk/port checks. |
| `--skip-packages` | Do not install OS packages (also skips the OS upgrade). |
| `--no-os-upgrade` | Do not upgrade the operating system before installing. |
| `--hwe-kernel` | Ubuntu only: also install the newer HWE kernel (`linux-generic-hwe-<version>`). |
| `--no-reboot` | Never reboot, even when the upgrade needs it. |
| `--no-systemd` | Do not install systemd units. |
| `--skip-frappe-docker` | Do not clone `frappe_docker` (this server cannot build images). |
| `--root DIR` | Install root (testing only). |
| `-y`, `--yes` | Never ask questions. |

See also the [install reference](../install.md). Re-running the installer is safe: it upgrades
the agent and keeps `agent.toml`, `db.env`, certificates and secrets.

### Operating-system upgrade at install time

Before it installs anything (right after the preflight checks) `install.sh` brings the server
up to date:

1. `apt-get update`, then `apt-get full-upgrade` (non-interactive, existing configuration files
   are kept). This also installs the newest kernel of the distribution series.
2. `apt-get autoremove`.
3. With `--hwe-kernel` (Ubuntu only) it also installs the HWE kernel `linux-generic-hwe-<version>`.

When the upgrade needs a reboot (a new kernel creates `/var/run/reboot-required`), the installer
**finishes the installation first** and then reboots the server automatically **1 minute later**.
In interactive mode it asks first (default *yes*). Benches, sites, the proxy and MariaDB come
back by themselves after the reboot (Docker restart policies and systemd units).

| Flag | Effect |
|---|---|
| `--no-os-upgrade` | Skip the OS upgrade. |
| `--skip-packages` | Skips packages **and** the OS upgrade. |
| `--hwe-kernel` | Ubuntu: also install the HWE kernel. |
| `--no-reboot` | Never reboot; reboot later yourself when convenient. |

## 5. First login and the Administration area

1. Open `https://<control-plane>/login` and log in as `Administrator`.
2. Open **Administration** in the sidebar (`/dashboard/admin`). Only platform admins
   (Administrator and System Managers) can open it; customers get *403*.

The page has seven tabs.

### 5.0 First-run setup checklist

On a new platform the Administration page starts with a setup checklist. Work through it in
this order; each item links to the place where you set it:

1. Platform name and domain (brand and root domain).
2. Currency (Billing).
3. A payment gateway (Stripe or Paymob).
4. Backup storage (a storage profile used for platform backups).
5. Outgoing e-mail (the control plane's own Email Account, and optionally the managed relay).
6. Plans and prices.
7. An external uptime probe.
8. A test site, to see the whole flow working.

### 5.1 Overview

Read-only health of the whole platform:

| Card | Shows |
|---|---|
| Servers | online / offline |
| Sites | count and number of teams |
| Failed jobs (24 h) | failed Agent Jobs, with desk links in the table below |
| Open tickets | and how many have their first response overdue |
| MRR | monthly recurring revenue of active subscriptions (billing-exempt teams excluded) |
| Overdue invoices | count |
| Outdated agents | servers behind the latest published Agent Release |
| Listings in review | marketplace listings waiting for you |
| Sites by status / By Frappe version | distribution |
| Server health | the fleet health panel (servers by health status), agent versions, **Disk alerts** (servers ≥ 85 % disk) |
| Recent platform changes | every change made in this area (who, what, when) |

### 5.2 Features & pricing

**Plans and add-ons.** Click **New plan** or **Edit** on a row.

| Field | Meaning |
|---|---|
| Title, Type | *Site*, *Server Hosted*, *Server BYOS* or *Add-on* |
| Price per month, Currency | what the customer pays |
| Max sites | 0 = unlimited |
| CPU seconds per day, Max storage (MB), Max database (MB) | site limits, 0 = unlimited |
| Over the limit | *Notify* (e-mail), *Throttle* (rate limit while over the CPU budget), *Suspend* (maintenance mode while over any limit) |
| Order | upgrade suggestions go up in this order |
| Enabled | **Disable** hides a plan from new choices |

Default plans after install: BYOS Server $10/month, Hosted Small $40/month, Site Basic $5/month.

**Offsite attachments, DR replica and trash bin.** Turn on *Offsite attachments*, *DR replica*,
*Trash bin*, decide whether *Customers may use their own buckets*, choose the default primary /
replica / trash buckets (storage profiles), *Replication interval (minutes)* (15), *Max deletions
per sync*, *Compare files by*, trash retention defaults, *Cache size per mount*, and the
**Add-on prices (per site and month)**.

**Backups and agents.** *Minimum / Maximum / Included retention (days)*, *Max local retention
(days)*, *Max monthly backups kept*, *Teams with the extended retention add-on*, *Agent upgrade
timeout (minutes)*, *Agent repository URL*, **Customers may upgrade the agent on their own
servers**, *Alert e-mail*, *Registry*, *Registry user*, *Registry password*.

!!! tip "Registry"
    Without a registry an image exists only on the server that built it. Set *Registry*
    (e.g. `ghcr.io/your-org`) so builds are pushed and every server can pull them.

**Billing.** *Billing enabled*, *Currency*, *Tax (%)*, *Invoice day*, *Grace days*, *Suspend
after (days)*, *BYOS server plan*, *Default site plan*, *Sign-up credits*, *Minimum top-up*,
*Maximum top-up*, *Return URL after payment*, and the payment gateways:

| Gateway | Fields | Webhook URL to register at the provider |
|---|---|---|
| **Stripe** | Enabled, Publishable key, secret key, webhook secret | `https://<cp>/api/method/nexaploy.api.billing.stripe_webhook` |
| **Paymob** | Enabled, Integration ID, iframe ID, Currency, Exchange rate, API base URL, API key, HMAC secret | `https://<cp>/api/method/nexaploy.api.billing.paymob_webhook` (processed and response callback) |

!!! note "Secrets"
    Secrets are never shown again. A stored secret shows `••••last4`. Leave the field empty to
    keep it; use the remove button to clear it.

How billing runs: on the *Invoice day* each team gets one invoice for the previous month. Credits
pay it first. It is due after *Grace days*; unpaid invoices then become **Overdue**, and after
*Suspend after (days)* the team's active sites are **Suspended** (maintenance mode). A payment
through Stripe/Paymob marks the invoice paid and lifts the suspension automatically.

### 5.3 Storage & backups

**Storage profiles (S3-compatible buckets).** **New profile** → *Profile ID*, *Title*,
*Provider* (AWS, IDrive, Wasabi, B2, R2, MinIO, Hetzner, Other), *Region label*, *Endpoint*,
*Region*, *Addressing* (preset default / path / virtual-hosted style), *Bucket*, *Path prefix*,
*Access key*, *Secret key*. Use **Test** to check a profile from a server (a write/delete/list
test); **Delete** removes it.

**Platform backup storage.** Choose a profile under *Use this storage profile for database
backups* and click **Apply**. Every server then uploads backups to that bucket under
`<prefix>/<server_id>/<bench>/<site>/<timestamp>`, so a backup taken on one server restores on
another.

**Google Drive, OneDrive and Dropbox apps.** Customers can send backups to their own cloud drive.
Create one OAuth app per provider and fill *Google client ID/secret*, *Microsoft client ID/secret*,
*Microsoft tenant* (`common` by default), *Dropbox app key/secret*. Register the **Redirect URI**
shown on the page (use **Copy**) with each provider:

```
https://<control-plane>/api/method/nexaploy.api.backup_destinations.oauth_callback
```

| Provider | Where | Scope |
|---|---|---|
| Google | Google Cloud Console → APIs & Services → Credentials → OAuth client (Web application) | `drive.file` (Nexaploy sees only files it creates) |
| Microsoft | Entra ID → App registrations → Web redirect URI | `offline_access Files.ReadWrite User.Read` |
| Dropbox | App Console → OAuth 2 → Redirect URIs | offline access |

### 5.4 E-mail & brands

**Managed e-mail relay.** Lets customer sites send mail through your authenticated SMTP relay.
Fill *Provider* (Generic SMTP, Amazon SES, Postmark, Mailgun, Brevo), *SMTP host*, *SMTP port*,
*Encryption*, *Username*, *Password*, *Default sender domain* (already verified at the provider),
*Daily send limit per site*, and the DNS guidance customers will see: *SPF include*, *DKIM
selector*, *DKIM record type*, *DKIM value* (`{domain}` and `{domain_dashed}` are replaced),
*DMARC policy*, *DMARC reports to*, *Return-Path host/value*. *Verify the SMTP login when sites
are configured* is recommended. Saving re-configures the relay on every site that uses it.

!!! warning "SPF, DKIM and DMARC for your own domain"
    Your *Default sender domain* must have SPF (include of your provider), DKIM (records from the
    provider) and a DMARC record such as `v=DMARC1; p=quarantine; rua=mailto:dmarc@example.com`.
    Otherwise mail lands in spam. The daily limit is written to the site config for information;
    enforce it at the relay provider.

**Brands (white label).** **New brand** → *Brand ID*, *Title*, *Primary colour*, *Logo URL*,
*Favicon URL*, *Support e-mail*, *Sender name*, *Sender address*, *Portal domains (one per line)*,
*Footer text*, *Platform default brand*. A request on a portal domain shows that brand on the
dashboard, sign-up and login pages; teams created there keep the brand for their e-mails.

!!! warning "Portal domains are not routed automatically"
    For each portal domain: create a DNS CNAME/A record to the control plane, add the host to the
    control plane site (`bench setup add-domain` or site config `domains`) **and** to the reverse
    proxy with a certificate. On all-in-one the control plane is a normal site, so
    `nexaploy site add-domain <cp domain> <portal domain> --wait` adds the route and certificate.
    The brand's *Sender address* must be sendable by the control plane's own outgoing Email Account.

### 5.5 Agent releases

1. Tag the repository (e.g. `v0.2.0`, see `scripts/release.sh`).
2. **New release** → *Version*, *Channel* (stable / beta), *Git ref* (default `v<version>`),
   optional *Repository URL*, or *Wheel URL* + *Wheel SHA-256*, *Release notes*.
3. **Publish**. *Unpublish* hides it again.
4. Under **Agents on servers** click **Preview outdated**, then **Upgrade all outdated servers**
   or **Upgrade** on one server.

On the server the job `agent.upgrade` builds a new venv next to the running one, checks it,
swaps it in and restarts both units 10 s after the job reported success. A watchdog rolls back
automatically when the new agent does not answer. Benches and sites are never touched. The
upgrade is *Failed* when no heartbeat with the new version arrives within the *Agent upgrade
timeout*. Manual rollback: **Roll back agent** on the server page, or on the server
`sudo nexaploy-agent rollback`. Details: [Agent upgrades](../operating.md#agent-upgrades).

### 5.6 Marketplace review

- **Listings waiting for review**: read the listing, write *Notes to the publisher* and click
  **Approve** or **Reject** (notes required). Approving creates one release per Frappe version
  from the app's `version-NN` branches.
- **Releases waiting for or failing verification**: every new release is test-built (Frappe +
  required apps + the app) before customers see it. **Test again** re-runs the build; **Override**
  → **Set status** marks it *Verified* / *Failed* by hand.
- **Reviews**: **Hide** a review (you are asked why) or **Show** it again.

Reviewer checklist: the repository is **public** (marketplace builds clone without credentials),
the app works on the generated versions, description/logo/screenshots are appropriate, the price
is sensible.

### 5.7 Teams & servers

- **Teams** (search): sites, servers, **Credits**, *Billing exempt*, *Extended retention*,
  *Enabled*. Buttons: **Suspend** / **Unsuspend** (billing suspension of all the team's sites) and
  **Credits** (grant credits; a negative amount is an adjustment; the reason appears in the team's
  credit ledger).
- **All servers**: team, status, disk, heartbeat, agent version, whether it hosts the control
  plane or is a build server, plus **pending OS updates** and **Reboot required**.

## 6. Servers: health, cleanup and OS updates

Open a server from **Servers**. The page shows *Server ID*, *Type*, *Last heartbeat*, *Agent
version* (with **Upgrade agent** / **Roll back agent**), *Plan*, *Facts* (CPU, memory, disks,
containers), benches and the last jobs with step output. A server is *Offline* after 5 minutes
without heartbeat.

### Server health

Every server has a health status:

| Status | Meaning |
|---|---|
| **OK** | nothing to do |
| **Warning** | something needs attention soon |
| **Critical** | act now (e.g. disk almost full, database down) |
| **Unknown** | no complete check yet |
| **Offline** | no heartbeat |

Below the status is a list of issues, each with a hint on how to fix it. Checked: disk space and
inodes, memory, load, failed systemd units, restarting or unhealthy containers, the database,
certificates, time synchronisation, pending reboot, outdated agent and stale backups. Click
**Run full check** to check now instead of waiting for the next heartbeat.

The admin **Overview** has a fleet **Server health** panel. A server that becomes *Critical*
sends a notification (event `server_health_critical`), and its recovery another one
(`server_health_ok`). Customers see a health badge on their own servers.

### Clean up disk space

**Clean up** on the server page frees space through the agent:

1. Choose the targets (all are selected by default):

    | Target | What is removed |
    |---|---|
    | Unused Docker images | images no container uses (old bench images) |
    | Build cache | Docker build cache |
    | Leftover containers | stopped one-off containers |
    | Container logs | oversized container logs; log rotation is configured |
    | Old bench/site logs | rotated logs in benches and sites |
    | Local backups | only copies **already uploaded** to backup storage, never the only copy |
    | Job history | old agent job records |
    | System journal | old `journald` entries |
    | apt cache | downloaded packages |
    | Temporary files | old temporary files |

2. Run a **dry run** first: it shows how much space each target would free.
3. Run the cleanup.

Site files, databases, certificates and configuration are **never** touched.

In the admin area you can set an automatic **weekly cleanup policy** and run **Clean up all
servers** at once. When a disk becomes critical, a safe cleanup runs automatically.

### System updates card

Each server page has a **System updates** card:

| Shows | Meaning |
|---|---|
| Pending updates | packages that can be upgraded, and how many are **security** updates |
| Kernel | the **running** kernel and the newest **installed** kernel |
| **Reboot required** | the server needs a reboot (e.g. a new kernel) |
| Last update | when the last update job ran |

Actions:

1. **Update now**: choose *all updates* or *security only*, and the reboot behaviour *never* or
   *if required*. The job runs on the server; when it needs a reboot and you chose *if required*,
   the reboot is scheduled **after the job reports success**.
2. **Reboot now**: reboots the server (also scheduled after the job reports success).
3. **Maintenance window**: *enabled*, *day*, *hour (UTC)*, and *reboot automatically if
   required*. Updates then run once a week in that window.
4. **Automatic security updates**: turns on `unattended-upgrades` on the server. It never
   reboots by itself; reboot in the maintenance window or with **Reboot now**.

Who can use it: platform admins on every server; customers (team Owner/Admin) on their own BYOS
servers.

!!! warning "All-in-one reboots"
    On an all-in-one server a reboot also restarts the control plane. The dashboard is
    unavailable for a few minutes; benches, sites, MariaDB, the proxy and the control plane come
    back by themselves (Docker restart policies + systemd units).

## 7. Uptime probes

Every active site gets a default uptime monitor automatically. Checks run from the built-in probe
(the control plane scheduler). On all-in-one that is the same host as the sites, and customers see
a warning. Add an external probe on a small VPS in another region:

1. Desk → **Uptime Probe** → New: name (e.g. `fra-1`), region. Copy the **token** (shown once;
   `rotate_token` issues a new one).
2. On the VPS:

    ```bash
    curl -fsSL -o /opt/nexaploy-probe \
      https://<control-plane>/api/method/nexaploy.api.monitoring_probe.download_script
    NEXAPLOY_URL=https://<control-plane> NEXAPLOY_PROBE_TOKEN=<token> python3 /opt/nexaploy-probe --once
    ```

3. Run it permanently with systemd (`/etc/systemd/system/nexaploy-probe.service`):

    ```ini
    [Service]
    Environment=NEXAPLOY_URL=https://cp.example.com NEXAPLOY_PROBE_TOKEN=...
    ExecStart=/usr/bin/python3 /opt/nexaploy-probe
    Restart=always
    DynamicUser=yes

    [Install]
    WantedBy=multi-user.target
    ```

    `systemctl enable --now nexaploy-probe`. The probe only needs outbound HTTPS.

With several probes a monitor goes *Down* only when a quorum (default: majority) agrees.
1-minute checks and more than 3 monitors per site need *advanced monitoring* (Team flag or the
site plan).

## 8. Support tickets

Customers open tickets at **Support**. Staff are users with the **Nexaploy Support** role (or
System Managers). In the desk open **Support Ticket**, use **Reply** (optionally *Mark as
Resolved*). SLA for the first response: Urgent 1 h, High 4 h, Normal 12 h, Low 24 h; the form shows
when it is due or overdue. Resolved tickets close automatically after 7 days. New tickets are
e-mailed to the assignee, else to every Nexaploy Support user, else to the *Alert e-mail*.

## 9. SaaS products and self sign-up

To sell "one-click ERPNext":

1. Prepare a **shared** bench group (no team) and deploy benches on Hosted servers without a team.
2. Desk → **SaaS Product**: *slug*, *title*, *release group*, *apps*, *default plan*, *trial days*
   (14), *domain* (default: the root domain), *enabled*.
3. Share `https://<cp>/signup/<slug>`. Visitors enter name, e-mail, site address and password,
   confirm their e-mail and get a team and a site on the least loaded bench.
4. When the trial ends and the team cannot pay, they get an e-mail; 3 days later the site is
   suspended. It is converted automatically once the team can pay.

## 10. Publishers and payouts

Teams publish their custom apps from **Publisher** and earn a share (default **80 %**, set per
publisher in the desk) of paid-app revenue. You review listings in **Marketplace review**.
Payouts are generated daily on *Invoice day + 5 days* for the previous month (cash basis, paid
invoices only) as **Draft** **Publisher Payout** documents. In the desk: **Approve** → **Mark
Paid** (with the bank/PayPal reference). A *Credits* payout adds the amount to the publisher's
prepaid credits.

## 11. Monitoring and alerts

| Alert | Rule | Who gets it |
|---|---|---|
| Disk almost full | disk > 85 % | team + *Alert e-mail*, at most every 6 h per server |
| Server offline | no heartbeat for 5 min | same |
| Unhealthy containers | container stopped or unhealthy | same |
| Server health critical / back to OK | health status changes to or from *Critical* | team channels |
| Site down / Monitor down | uptime incident opened | team channels |
| Job failed, Backup failed | user-facing jobs | team channels |
| Invoice issued, Payment failed, Sites suspended | billing | billing members |

Teams choose channels (e-mail, Telegram, WhatsApp, Slack, Discord, webhook) in **Notifications**.
The *Alert e-mail* always receives infrastructure alerts.

## 12. Daily operations

**Every day**

1. Administration → **Overview**: the **Server health** panel (any *Critical* or *Warning*
   server), offline servers, disk alerts, *Failed jobs (24 h)*, overdue tickets.
2. Open each failed job (desk link), read the failed step, fix the cause and retry (see
   [Troubleshooting](#16-troubleshooting)).
3. Answer support tickets before their SLA.

**Every week**

1. **All servers**: pending OS updates and *Reboot required*; use **Update now** or rely on the
   **Maintenance window**.
2. Disk usage: **Clean up** servers that keep growing, or rely on the weekly cleanup policy.
3. **Outdated agents**: publish and roll out agent releases.
4. **Marketplace review**: listings and failed release verifications.
5. Check that backups reach the bucket (Site Backups with status *Success*) and do a test restore
   of one site into a new site.

**Every month**

1. After the *Invoice day*: overdue invoices, suspended teams.
2. *Invoice day + 5*: approve and pay publisher payouts.

## 13. Upgrading the platform

| What | How |
|---|---|
| Agent | Agent releases (section 5.5) |
| Server infrastructure (proxy/DB compose, systemd units, OS) | re-run `install.sh` on the server (keeps secrets); OS: **Update now** |
| Control plane, hosted | `cd ~/frappe-bench && bench update --apps nexaploy` or `git pull` in the app then `bench --site <cp> migrate` and restart |
| Control plane, all-in-one | **Update control plane** in the admin area, or `sudo nexaploy controlplane update [--image IMG]` on the server |
| Customer benches | deploy a new image of the bench group; **Safe update** per site |

**Update control plane** (all-in-one) runs these steps: backup of the control plane → pull the
new image → switch the control plane bench to it → `migrate` → health check. If the health
check fails, it rolls back to the previous image automatically. From the command line:

```bash
sudo nexaploy controlplane update                      # newest image
sudo nexaploy controlplane update --image ghcr.io/a7733mai/nexaploy-control-plane:<version>
```

The dashboard is unavailable for a short time during the switch; sites keep running.

!!! warning
    Read `CHANGELOG.md` before an upgrade. On a hosted control plane, back it up first (next
    section).

## 14. Backing up the control plane

- **All-in-one**: a daily task backs up the control plane site through the agent; it is
  uploaded once platform backup storage is set. A manual backup:

    ```bash
    sudo nexaploy controlplane backup --to /root/cp-backup     # a full control plane backup in a folder
    sudo nexaploy site backup cloud.example.com --wait         # the same through a normal site backup
    sudo nexaploy site backups cloud.example.com
    ```

    Copy the folder off the server (e.g. `scp` or `rclone`). It is what
    `install.sh --restore-control-plane` needs.

- **Hosted**: use Frappe's own backups on the control plane bench, e.g. a cron job with
  `bench --site cp.example.com backup --with-files`, and copy the files off the host.
- Also keep `/opt/nexaploy/config/` of each server (agent token, DB root password) in a password
  manager or vault; it is not in the site backups.

## 15. Disaster recovery playbook

!!! warning "Practise first"
    Run these steps on a test server before you need them.

**A customer site is broken or data was deleted**

1. Site → **Backups → Restore to a point in time** into a *new site*, compare, then restore in
   place if needed; or **Backups → Restore…** with *Database only* / *Files only*.

**A server is lost** (requires backup storage in a bucket)

1. Install a new server and register it (same team).
2. Deploy the same bench groups on it (**Deploy to server**).
3. For each site: **Backups → Restore… → Restore as new site** on the new bench (the bench's
   server must read the same storage), or ask the team to do it.
4. Point the DNS of every domain to the new server; certificates are issued automatically.
5. Archive the old server. The binlog archive gives a recovery point of about 15 minutes for
   point-in-time restores of databases.

**The hosted control plane is lost**

Servers keep running: sites stay online and agents keep retrying. Restore the latest control plane
backup on a new Frappe bench with the **same domain** (`bench --site cp.example.com restore …
--with-public-files … --with-private-files …`), then `migrate`. Server tokens live in the restored
database, so agents reconnect by themselves once DNS points to the new host.

**Moving the control plane to a new server, or the all-in-one server is lost**

You need a control plane backup (`nexaploy controlplane backup --to <dir>`, or the daily backup
in the bucket) copied off the old server.

1. Copy the backup to the new server.
2. Install with it:

    ```bash
    curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
      | sudo bash -s -- --with-control-plane cloud.example.com --acme-email ops@example.com \
          --restore-control-plane /root/cp-backup --skip-dns-check
    ```

    (`--skip-dns-check` because DNS still points to the old server.)
3. Switch the DNS record of the control plane domain to the new server. The certificate is
   issued once DNS has changed.
4. Other servers keep working the whole time and reconnect to the control plane by themselves.
5. If the old all-in-one server is gone, its customer sites are gone too: restore them from the
   bucket on a server as described in *A server is lost*.

## Security checklist

- `--acme-email` is a real address; ports 80/443 open, everything else closed (no SSH from the
  internet, or key-only SSH).
- Administrator password changed after install; `control-plane.env` kept secret (mode 600).
- 2FA for every staff user (System Settings) and, for customers, *Require two-factor
  authentication* on teams that need it.
- Backup storage is **off the server**, and test restores are done regularly.
- OS updates applied weekly (**Maintenance window** + **Automatic security updates**).
- Every server shows health **OK**; notifications for `server_health_critical` reach you.
- Agents on the latest stable release.
- Stripe/Paymob webhook secrets set; only HTTPS webhook URLs.
- SPF/DKIM/DMARC on the relay sender domain.
- Only a few users have System Manager; review **Recent platform changes** and team activity.
- Marketplace apps reviewed (public repo, sensible code) before approving.
- An external uptime probe in another region.

More: [Security model](../security.md).

## 16. Troubleshooting

### Where to look

```bash
nexaploy ping                                   # agent API up? which version?
nexaploy jobs --status failure                  # failed jobs on this server
nexaploy jobs <job id>                          # every step with output and the error
journalctl -u nexaploy-agent-worker -n 200 -f   # jobs, heartbeat, polling
journalctl -u nexaploy-agent-api -n 200         # local API
docker ps --format '{{.Names}}\t{{.Status}}'    # nexaploy-db, nexaploy-traefik, <bench>-*
docker logs nexaploy-traefik --tail 100         # TLS / routing
docker logs nexaploy-db --tail 100              # MariaDB
cat /var/log/nexaploy-install.log               # servers set up by cloud-init / images
```

### Common problems

| Symptom | Cause and fix |
|---|---|
| `port 80 is already in use` during install | Another web server (nginx, Apache, Caddy) listens. Stop it or use a fresh server. |
| Browser shows Traefik's default certificate, TLS status *Default Certificate* | DNS does not point to this server, port 80 blocked, `--acme-email` still `admin@example.com`, or Let's Encrypt rate limit. Check `docker logs nexaploy-traefik` for `acme`. `acme.json` must be mode 600, owner 3100. |
| v16 sites cannot connect to the DB, TLS/SSL errors in `bench new-site` | The v16 MariaDB 11.8 client requires TLS. Re-run `install.sh` (DB becomes `mariadb:11.8`; 10.6 data upgrades in place). If you set `MARIADB_IMAGE`, use 11.4+. |
| Disk full, MariaDB stopped | **Clean up** on the server page (dry run first); by hand: `df -h /opt/nexaploy`, `docker system df`, `docker image prune`; reduce local backup retention; set remote storage. |
| Health *Critical* | Open the server page; each issue has a hint. Fix it, then **Run full check**. |
| Control plane domain check fails during install | The domain does not resolve to this server yet: fix the DNS record and wait, or use `--skip-dns-check`. |
| Server *Offline* | `systemctl status nexaploy-agent-worker`; `401` in the journal = wrong token (archived/rotated): re-register with a new token and `--force-register`. |
| A job failed | Local jobs: fix the cause and `nexaploy retry <job id> --wait` (successful steps are skipped). Control plane jobs: desk → Agent Job → **Retry**. A failed move continues when its failed job is retried. |
| Image cannot be pulled on another server | Configure the *Registry* (Features & pricing → Backups and agents). |
| Agent upgrade failed | The watchdog rolls back after ~3 minutes; manual `sudo nexaploy-agent rollback`. |
| Server stays *Reboot required* | Use **Reboot now**, or let the **Maintenance window** reboot it. |

### Frappe v16 notes

- New bench groups default to v16 (Python 3.14, Node 24).
- `--no-mariadb-socket` is deprecated; the agent uses `--mariadb-user-host-login-scope=%` and
  falls back only for v14.
- The official v16 images bake assets in `./assets` and run as `1000:0`; production images from
  `image-builder/` follow the Nexaploy contract (UID 3113).

## 17. CLI reference

Both commands are installed in `/usr/local/bin` on every server. Run them as root.

### `nexaploy` (talks to the local agent API)

Global options: `--url`, `--token` (or `NEXAPLOY_URL` / `NEXAPLOY_TOKEN`), `--config/-c`,
`--json`. Mutating commands print the job id; `--wait/-w` follows the job.

| Command | Purpose |
|---|---|
| `nexaploy ping` | agent version and server id |
| `nexaploy server` | host facts |
| `nexaploy jobs [ID] [--status S] [--kind K] [--limit N] [--wait]` | list jobs / show or follow one |
| `nexaploy cancel ID` | cancel a queued job |
| `nexaploy retry ID [--wait]` | retry a failed job from the failed step |
| `nexaploy build --tag T --major 14\|15\|16 --apps-json FILE [--frappe-branch B] [--push]` | build a bench image |
| `nexaploy bench list` | benches |
| `nexaploy bench create NAME --image TAG [-e KEY=VAL] [--apps-json F]` | create a bench |
| `nexaploy bench update NAME --image TAG` | new image + migrate all sites |
| `nexaploy bench start\|stop\|restart NAME` | control a bench |
| `nexaploy bench remove NAME [--force]` | remove a bench |
| `nexaploy site list [--bench B]` | sites |
| `nexaploy site create SITE --bench B [-a APP]…` | new site (asks for the admin password, or `NEXAPLOY_ADMIN_PASSWORD`) |
| `nexaploy site install-app SITE APP` / `uninstall-app SITE APP` | apps |
| `nexaploy site add-domain SITE DOMAIN` / `remove-domain SITE DOMAIN` | domains |
| `nexaploy site backup SITE [--no-files] [--no-upload]` | backup |
| `nexaploy site backups SITE` | local and remote backups |
| `nexaploy site restore SITE --backup-id TS \| --remote-path PATH [--bench B]` | restore |
| `nexaploy site migrate SITE` | `bench migrate` |
| `nexaploy site set-config SITE KEY VALUE [-p]` | site config (`-p` parses JSON) |
| `nexaploy site drop SITE [--skip-backup] [-y]` | delete a site (backup first) |
| `nexaploy controlplane update [--image IMG]` | all-in-one: update the control plane (backup, switch, migrate, health check, rollback on failure) |
| `nexaploy controlplane backup --to DIR` | all-in-one: back up the control plane into a folder |

### `nexaploy-agent` (the daemons and maintenance)

| Command | Purpose |
|---|---|
| `nexaploy-agent api [--host] [--port]` | the local API (systemd unit `nexaploy-agent-api`) |
| `nexaploy-agent worker` | the job worker, heartbeat and polling (`nexaploy-agent-worker`) |
| `nexaploy-agent register --control-plane URL --token T [--site-name D]` | register with a control plane |
| `nexaploy-agent version` | print the version |
| `nexaploy-agent upgrade --version X [--ref R] [--repo-url U] [--wheel-url W --wheel-sha256 H] [--no-restart]` | queue a local agent upgrade (standalone servers) |
| `nexaploy-agent rollback [--no-restart]` | put the previous agent version back |

If even the new CLI does not start:
`sudo /opt/nexaploy/agent/.venv.prev/bin/python -m nexaploy_agent.agent_cli rollback`.
