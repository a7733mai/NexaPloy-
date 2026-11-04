# Changelog

All notable changes to Nexaploy are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). One version covers the whole repository:
the agent (`agent/nexaploy_agent/__init__.py`) and the control plane
(`control-plane/nexaploy/__init__.py`) are released together by `scripts/release.sh`.

## [0.1.0] - Unreleased

First release.

### Added

- **Servers.** Hosted servers (created on Hetzner Cloud or DigitalOcean from the dashboard,
  bootstrapped by cloud-init) and BYOS servers (the customer runs `install.sh` with a
  one-time registration token). Both run the same outbound-only agent: no inbound ports
  or SSH needed. Removing the agent leaves sites running.
- **Agent** (`nexaploy-agent`): FastAPI on 127.0.0.1 plus a SQLite job worker. It runs
  resumable step-by-step jobs and sends a heartbeat with host facts. The `nexaploy` CLI
  covers benches, sites, backups, domains and job retry (`nexaploy retry <job>`).
  `install.sh` sets up Docker, users with fixed UIDs, the `/opt/nexaploy` tree,
  networks, the agent and systemd units.
- **Benches.** One bench per Release Group (Frappe major + apps), hardened Docker Compose,
  per-bench Redis. Frappe v14, v15 and v16 can run side by side. One shared MariaDB
  (`mariadb:11.8` LTS) per server. Traefik v3 proxy with Let's Encrypt and one route
  file per site.
- **Images.** One Docker image per Release Group built by `image-builder/` (optionally
  pushed to a registry and pulled by other servers).
- **Sites.** Create, install/uninstall apps, custom domains with automatic TLS,
  migrate, site config editor, drop. Staging copies (scrubbed, not billed), renames,
  moves between benches and servers, and upgrades to a newer Frappe major
  (e.g. v15 → v16 by backup, restore and migrate). Transfers between teams.
- **Backups.** Scheduled or manual backups to S3-compatible storage (rclone), with
  checksums, retention, restore and short-lived download links.
- **Marketplace.** Apps with per-major releases, paid apps, publishers, listing review
  and monthly publisher payouts. Custom apps from GitHub (GitHub App).
- **Billing.** Plans, subscriptions, monthly invoices, Stripe and Paymob, prepaid
  credits. Suspension for unpaid invoices is lifted when the customer pays.
- **Teams.** Owner/Admin/Developer/Billing roles, invitations, 2FA requirement,
  activity log and API keys.
- **Operations.** Monitoring facts and metrics, disk/offline/unhealthy-container alerts,
  uptime checks, auto updates inside an update window, per-site usage (CPU, database,
  storage) with plan limits (notify, throttle or suspend).
- **Site insights.** Logs, request analytics, slow queries, read-only SQL and an
  admin-only console.
- **Customer portal** (`/dashboard`) in English and Arabic (RTL). SaaS signup pages
  with trials. Support tickets.
- **Agent upgrades from the control plane.** Agent Releases (stable/beta channels).
  Upgrade or roll back one server, or all outdated servers at once. The agent keeps
  the previous version and rolls back by itself if the new one does not start.
- **Release tooling.** `scripts/release.sh`, a GitHub Actions release workflow (tests,
  control plane image on ghcr.io, GitHub Release), Packer templates for Hetzner Cloud and
  DigitalOcean server images, and the documentation site (English and Arabic).

[0.1.0]: https://github.com/a7733mai/NexaPloy-/releases/tag/v0.1.0
