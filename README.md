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
| [`docs/`](docs/) | User documentation, English + Arabic (MkDocs Material, published to GitHub Pages) |
| [`packer/`](packer/) | Hetzner Cloud / DigitalOcean server images with the agent pre-installed |
| [`scripts/release.sh`](scripts/release.sh), [`CHANGELOG.md`](CHANGELOG.md) | Releases: version bump + tag; `.github/workflows/release.yml` builds and publishes |

## Deploy the control plane in 5 minutes

One fresh Ubuntu 22.04/24.04 server (2 GB RAM, 20 GB disk), ports 80 and 443 open, and a DNS
**A record** `cp.example.com -> <server IP>`. Then:

```bash
curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
  | sudo bash -s -- --with-control-plane cp.example.com --acme-email you@example.com
```

No other flags are needed. The installer updates the server, checks that `cp.example.com` points
to it (and tells you exactly which record to create if not), installs Docker and the agent, pulls
the published control plane image (`ghcr.io/a7733mai/nexaploy-control-plane:<version>`, `:16` or
`:latest`; when none is available it builds one from the official ERPNext image, ~10 minutes once),
creates the control plane site with a Let's Encrypt certificate and registers the server to it.
Run it with no options at all for an interactive setup.

Open `https://cp.example.com/dashboard/admin` (user `Administrator`, password in
`/opt/nexaploy/config/control-plane.env`) and follow the **setup checklist**.

- **Update:** Administration > Overview > *Update control plane* (backup, new image, migrate, health
  check, automatic rollback), or on the server: `sudo nexaploy controlplane update`.
- **Backup:** `sudo nexaploy controlplane backup --to /root` (one tarball: database, files, encryption key).
- **Move to a new server:** `install.sh --with-control-plane cp.example.com --restore-control-plane
  /root/nexaploy-control-plane-....tar` (also an `https://` URL or `remote:path`), then point the DNS
  record to the new server; the other servers reconnect by themselves.

### نشر لوحة التحكم في 5 دقائق

خادم Ubuntu 22.04/24.04 جديد (ذاكرة 2 GB وقرص 20 GB)، والمنفذان 80 و443 مفتوحان، وسجل DNS من نوع
**A** يشير من `cp.example.com` إلى عنوان الخادم. ثم:

```bash
curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
  | sudo bash -s -- --with-control-plane cp.example.com --acme-email you@example.com
```

لا حاجة لأي خيارات أخرى: يحدّث المثبّت الخادم، ويتحقق من أن النطاق يشير إليه (ويخبرك بالسجل
المطلوب إن لم يكن كذلك)، ويثبّت Docker والوكيل، وينزّل صورة لوحة التحكم المنشورة (أو يبنيها من صورة
ERPNext الرسمية إن لم تتوفر، نحو 10 دقائق لمرة واحدة)، وينشئ موقع اللوحة بشهادة Let's Encrypt.
افتح `https://cp.example.com/dashboard/admin` (المستخدم `Administrator`، وكلمة المرور في
`/opt/nexaploy/config/control-plane.env`) واتبع **قائمة الإعداد**.

- **التحديث:** الإدارة > نظرة عامة > *تحديث لوحة التحكم* (نسخة احتياطية ثم صورة جديدة ثم ترحيل ثم
  فحص سلامة مع تراجع تلقائي)، أو على الخادم: `sudo nexaploy controlplane update`.
- **النسخ الاحتياطي:** `sudo nexaploy controlplane backup --to /root`.
- **النقل إلى خادم جديد:** `install.sh --with-control-plane cp.example.com --restore-control-plane <ملف أو رابط>`
  ثم وجّه سجل DNS إلى الخادم الجديد؛ تعود الخوادم الأخرى للاتصال تلقائياً.

## Quick start (BYOS server)

```bash
curl -fsSL https://<control-plane>/install.sh | sudo bash -s -- --token <REGISTRATION_TOKEN> --control-plane https://<control-plane>
```

Standalone (no control plane) — see `agent/README.md`.

Documentation: https://a7733mai.github.io/NexaPloy-/ (source in `docs/`, `mkdocs serve` to preview).

Usage guides (English and Arabic):

- [Operator guide](docs/guide/operator.md) ([العربية](docs/guide/operator.ar.md)): install, Administration area,
  pricing, storage, e-mail, branding, agent releases, server health and OS updates, operations, disaster recovery, CLI.
- [Customer guide](docs/guide/customer.md) ([العربية](docs/guide/customer.ar.md)): teams, servers, bench groups,
  sites, domains, backups, safe updates, monitoring, notifications, billing, support.
