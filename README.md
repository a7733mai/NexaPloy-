# Nexaploy

Hosting & management platform for **Frappe / ERPNext** — a small, focused alternative to
Frappe Cloud. Host sites on our servers, or **bring your own server** (BYOS) and let
Nexaploy manage it for a flat monthly fee.

- Multiple Frappe majors side by side (v14, v15, v16 …) — one bench per Release Group.
- App marketplace: apps declare which Frappe majors they support.
- Automated backups to S3-compatible storage (rclone), restore with checksum verification.
- Automatic TLS with Traefik + Let's Encrypt, custom domains per site.
- Outbound-only agent: no inbound ports or SSH needed on customer servers. Removing the agent leaves sites running.

## Repository

| Path | What |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Contracts every component follows — read first |
| [`bench-template/`](bench-template/) | Hardened Docker Compose for a bench, shared MariaDB, Traefik proxy |
| [`image-builder/`](image-builder/) | One Docker image per Release Group (Frappe major + apps) |
| [`agent/`](agent/) | `nexaploy-agent` (FastAPI + SQLite jobs), `nexaploy` CLI, `install.sh` |
| [`control-plane/`](control-plane/) | Frappe app `nexaploy`: servers, marketplace, release groups, sites, backups, plans |
| [`ROADMAP.md`](ROADMAP.md) | Phases |

## Quick start (BYOS server)

```bash
curl -fsSL https://<control-plane>/install.sh | sudo bash -s -- --token <REGISTRATION_TOKEN> --control-plane https://<control-plane>
```

Standalone (no control plane) — see `agent/README.md`.
