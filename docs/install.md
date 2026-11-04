# Install reference

`install.sh` (in the repository at `agent/install/install.sh`) prepares a server and installs the agent.
Run it as root. You can run it again at any time: a second run upgrades the agent and keeps
the existing configuration, secrets and certificates.

```bash
curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
  | sudo bash -s -- [options]
```

## Options

| Option | Meaning |
|---|---|
| `--control-plane URL` | Base URL of the control plane, e.g. `https://cp.example.com`. Leave it out for a standalone server. |
| `--token TOKEN` | With `--control-plane`: the one-time **registration token**. Without it: the bearer token for the local agent API (random by default). |
| `--server-id ID` | Server id (`^[a-z0-9][a-z0-9-]{1,62}$`). Default: assigned by the control plane, or generated. Used for hosted servers created by hand: `--server-id <id> --token <server token>`. |
| `--acme-email EMAIL` | E-mail of the Let's Encrypt account used by the proxy. Set it: the default `admin@example.com` triggers a warning. |
| `--agent-source SRC` | Git URL or local path of the Nexaploy repository (or its `agent/` folder). Default: the official GitHub repository. |
| `--branch BRANCH` | Git branch or tag for `--agent-source` (default `main`). Use a release tag such as `v0.1.0` in production. |
| `--frappe-docker-ref REF` | `frappe_docker` git ref used as the image build context (default `main`). |
| `--force-register` | Register again even though a server token is already stored. |
| `--skip-preflight` | Skip the architecture, RAM, disk and port checks. |
| `--root DIR` | Install root (default `/opt/nexaploy`). Other paths are meant for testing. |
| `--skip-packages` | Do not install OS packages. Docker, the compose plugin, Python ≥ 3.11, rclone, git and jq must already be installed. |
| `--no-systemd` | Do not install the systemd units. You then run `nexaploy-agent api` and `nexaploy-agent worker` yourself. |
| `--skip-frappe-docker` | Do not clone `frappe_docker`. This server cannot build images. |
| `--with-control-plane DOMAIN` | **New.** All-in-one: also installs the control plane on this server for `DOMAIN`. |
| `-h`, `--help` | Show the help. |

## What it does

1. **Preflight.** Checks for Ubuntu 22.04/24.04 (Debian 12 best effort), x86_64 or aarch64,
   at least 2 GB RAM, at least 20 GB free under the install root (first install only), and
   free ports 80/443. A port held by Nexaploy's own proxy is fine.
2. **Packages.** Installs Docker CE with the compose plugin (from Docker's repository), git,
   jq, acl, rclone, openssl, and Python ≥ 3.11 with venv.
3. **Users and directories.** Creates system users with fixed UIDs: 3100 Traefik, 3101 MariaDB,
   3102 Redis, 3113 Frappe. Creates the `/opt/nexaploy` tree with matching owners.
4. **Networks.** Creates the Docker networks `proxy-network` and `nexaploy-db`.
5. **Agent.** Copies or clones the source to `/opt/nexaploy/agent/src`, bundles the bench
   templates, and installs the agent into `/opt/nexaploy/agent/.venv`. Links `nexaploy` and
   `nexaploy-agent` into `/usr/local/bin`. Copies `image-builder/` to `/opt/nexaploy/images/` and
   clones `frappe_docker`.
6. **Configuration.** Writes `/opt/nexaploy/config/agent.toml` (mode 600, existing values kept),
   `db.env` (a random MariaDB root password), and an empty `rclone.conf`. With
   `--control-plane` and `--token`, it registers the server. The control plane then sends the
   backup storage settings.
7. **Services.** Installs and starts `nexaploy-agent-api` and `nexaploy-agent-worker`, then
   starts the shared MariaDB (`nexaploy-db`) and the proxy (`nexaploy-proxy`).
8. **Summary.** Prints the version and the result of `nexaploy ping`.

## Files on a server

```
/opt/nexaploy/
├── agent/            src/ (source), .venv/ (agent), data/jobs.db (job queue)
├── config/           agent.toml, db.env, rclone.conf      (mode 600)
├── proxy/            Traefik: docker-compose.yml, config/sites/<site>.yml, data/certificates/acme.json
├── db/               MariaDB 11.8: docker-compose.yml, data/
├── images/           build.sh, Containerfile, frappe_docker/, builds/<tag>/
├── benches/<bench>/  docker-compose.yml, .env, sites/, logs/
└── backups/          staging area before upload
```

## Services and CLI

| Unit | Command | Purpose |
|---|---|---|
| `nexaploy-agent-api` | `nexaploy-agent api` | Local REST API on `127.0.0.1:8787` (bearer token from `agent.toml`) |
| `nexaploy-agent-worker` | `nexaploy-agent worker` | Runs jobs one at a time. Sends the heartbeat and polls the control plane for jobs |

```bash
nexaploy ping                       # is the agent API up? (prints the version)
nexaploy jobs [ID] [--wait]         # list jobs / follow one
nexaploy retry <job id> --wait      # retry a failed local job from the failed step
nexaploy bench list | site list
nexaploy-agent version | upgrade --version X | rollback
```

## Server images

`packer/` builds Ubuntu 24.04 snapshots for Hetzner Cloud and DigitalOcean. They come with
Docker, the agent and the main images already pulled. On first boot, the install finishes
from the user-data:

| User-data | Result |
|---|---|
| `NEXAPLOY_CONTROL_PLANE=…` + `NEXAPLOY_TOKEN=…` | the server joins that control plane |
| `NEXAPLOY_DOMAIN=…` | all-in-one (new) |
| `NEXAPLOY_ACME_EMAIL=…` | Let's Encrypt e-mail (optional) |

Without user-data, run `sudo nexaploy-firstboot --control-plane URL --token T` later. See
`packer/README.md` for details.

## Uninstall

```bash
sudo systemctl disable --now nexaploy-agent-api nexaploy-agent-worker
```

Benches, sites, MariaDB and the proxy keep running: they are plain Docker Compose projects in
`/opt/nexaploy`. You can manage them with `docker compose` from then on.
