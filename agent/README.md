# nexaploy-agent

The per-server agent of Nexaploy (component C, see `../ARCHITECTURE.md` §5). It runs on
every server (hosted or BYOS), owns `/opt/nexaploy`, and drives Docker Compose to run
Frappe benches and sites. It exposes a local REST API, executes jobs serially from a
SQLite queue, and optionally talks outbound to the control plane.

```
nexaploy CLI ──HTTP──► nexaploy-agent api  ──┐
                                             ├── jobs.db (SQLite, WAL)
control plane ◄─HTTPS─ nexaploy-agent worker ┘   └─► docker compose / bench / rclone / build.sh
```

Two processes, both systemd units:

| Unit | Command | Does |
|---|---|---|
| `nexaploy-agent-api` | `nexaploy-agent api` | FastAPI on `127.0.0.1:8787`; validates requests and enqueues jobs (`202 {job_id}`) |
| `nexaploy-agent-worker` | `nexaploy-agent worker` | Runs one job at a time; heartbeat / poll / job_update threads when a control plane is configured |

## Install on a server

```bash
# standalone
curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
  | sudo bash -s -- --acme-email ops@example.com

# BYOS: register with the control plane using a one-time registration token
curl -fsSL .../agent/install/install.sh | sudo bash -s -- \
  --control-plane https://cloud.example.com --token <registration-token> --acme-email ops@example.com
```

Flags: `--token`, `--control-plane`, `--server-id`, `--acme-email`, `--agent-source <git URL|path>`,
`--branch`, `--frappe-docker-ref`, `--force-register`, `--skip-preflight`. The script is idempotent:
re-running it upgrades the agent and keeps `agent.toml`, `db.env` and certificates. It:

1. checks OS, arch, RAM ≥ 2 GB, disk ≥ 20 GB, ports 80/443;
2. installs Docker CE + compose plugin, git, python ≥ 3.11 venv, rclone, jq, acl;
3. creates the fixed-UID users (3100 traefik, 3101 mariadb, 3102 redis, 3113 frappe) and the `/opt/nexaploy` tree;
4. creates the `proxy-network` and `nexaploy-db` networks;
5. installs the agent into `/opt/nexaploy/agent/.venv` (source in `/opt/nexaploy/agent/src`), copies
   `image-builder/{build.sh,Containerfile,versions.json}` to `/opt/nexaploy/images/` and clones frappe_docker;
6. writes `agent.toml` (random local API token), `db.env` (random root password), empty `rclone.conf`,
   proxy/db `.env` from `env.example`, and registers with the control plane if asked;
7. installs and (re)starts the systemd units, starts the `nexaploy-db` and `nexaploy-proxy` stacks;
8. prints `nexaploy ping`.

Removing the agent (`systemctl disable --now nexaploy-agent-{api,worker}`) leaves benches,
sites, the DB and the proxy running.

## Configuration (`/opt/nexaploy/config/agent.toml`, override with `NEXAPLOY_CONFIG`)

```toml
[agent]
server_id = "srv-ab12"
token = "..."            # bearer token for the local API
bind = "127.0.0.1"
port = 8787
root = "/opt/nexaploy"

[control_plane]
url = ""                 # empty => standalone
token = ""
heartbeat_seconds = 60
poll_seconds = 10

[backup]
remote = "nexaploy-backups"
prefix = "srv-ab12"
retention_days = 14       # local; the newest 3 are always kept (keep_min)

# optional
[bench]                   # defaults for new bench .env files
tz = "UTC"
backend_workers = 2
[images]
build_script = "/opt/nexaploy/images/build.sh"
```

Backups upload with `rclone --config /opt/nexaploy/config/rclone.conf` to the remote named by
`backup.remote`; until that section exists in `rclone.conf` backups stay local (the job logs a warning).
With a control plane the remote is pushed by the `server.configure_backups` job (queued as soon as
the server is Active and whenever the storage settings change): it writes `[<remote>-s3]` (S3
credentials) and `[<remote>]` (an `alias` of `<remote>-s3:<bucket>`) into `rclone.conf` (mode 600,
other remotes kept, unchanged file not rewritten) and `remote`/`prefix` into agent.toml `[backup]`.
The worker applies it immediately; restart `nexaploy-agent-api` for `GET /v1/sites/{s}/backups` to
use a changed prefix.

### Private registry

`image.build`, `bench.create` and `bench.update` accept `registry: {url, username, password}`.
`image.build` with `push: true` runs `docker login <host> --username U --password-stdin` (the
password is written to stdin only — never argv, logs or `jobs.db`) and then `build.sh --push`
(an image already present locally is `docker push`ed instead). Bench jobs log in only when the image
has to be pulled. Credentials are stored in root's `~/.docker/config.json` by `docker login`.

## CLI

```
nexaploy ping | server | jobs [ID] [--wait] | cancel ID
nexaploy build --tag nexaploy/bench:erp-v15-main-20261103T2130 --major 15 --apps-json apps.json --wait
nexaploy bench list | create NAME --image TAG [-e KEY=VAL] | update NAME --image TAG
nexaploy bench start|stop|restart NAME | remove NAME [--force]
nexaploy site list [--bench B] | create SITE --bench B [-a erpnext] | install-app SITE APP
nexaploy site add-domain SITE DOMAIN | remove-domain SITE DOMAIN | migrate SITE | set-config SITE KEY VALUE [-p]
nexaploy site backup SITE [--no-files] [--no-upload] | backups SITE
nexaploy site restore SITE --backup-id TS | --remote-path PATH [--bench B] | drop SITE [--skip-backup]
```

Every mutating command prints the job id; `--wait` follows the job and streams step output.
URL/token come from `agent.toml` (or `--url/--token`, `NEXAPLOY_URL/NEXAPLOY_TOKEN`).

## Jobs

| Kind | Params | Steps |
|---|---|---|
| `image.build` | `major, apps_json, tag, frappe_branch?, python?, node?, push?, registry?` | prepare, build (login if pushing; skipped if the image exists, then only pushed) |
| `bench.create` | `name, image, env?, apps_json?, registry?` | prepare (dirs, networks, `.env`, compose), ensure_image (login + pull), configure, start |
| `bench.start/stop/restart` | `bench` | one step |
| `bench.update` | `bench, image, registry?` | ensure_image, write_config, configure, start, migrate_sites |
| `bench.remove` | `bench, force?` | check, down, remove_routes, remove_files (archived to `benches/.archived/` when forced with sites) |
| `site.create` | `bench, name, admin_password, apps?` | check, new_site, install_apps, enable_monitor, write_route |
| `site.install_app` / `site.uninstall_app` | `site, app` | install / uninstall |
| `site.add_domain` / `site.remove_domain` | `site, domain` | add/remove domain in site_config, write_route |
| `site.backup` | `site, with_files=true, upload=true` | backup, stage (manifest + sha256), upload, prune |
| `site.restore` | `site, backup_id \| remote_path, bench?, with_files?` | locate, fetch, verify, restore, restore_config (encryption_key), migrate, write_route |
| `site.migrate` | `site` | migrate |
| `site.set_config` | `site, key, value` | set_config |
| `site.drop` | `site, skip_backup?` | backup, drop, remove_route |
| `server.configure_backups` | `remote, type=s3, provider, endpoint?, region?, bucket, access_key_id, secret_access_key, prefix?, retention_days?` | write_rclone_conf, update_agent_config |
| `server.health` | — | collect (result = host facts incl. containers / bench health) |
| `backup.prune_remote` | `site, bench, keep_days=14, keep_min=3, prefix?` | list (`rclone lsf --dirs-only`, select dated dirs older than keep_days, newest keep_min kept), prune (`rclone purge` each) |

Params are the REST body plus path parameters, identical for jobs sent by the control plane.
Every step is check-then-act, so a job interrupted by a crash is re-queued at startup and resumes:
successful steps are skipped. The first failing step fails the job and the rest are `skipped`.
Values under keys matching `password|secret|token|key` are redacted in API output, masked in step
output, and scrubbed from `jobs.db` once the job finishes.

All `bench …` commands run inside the bench (`docker compose -p <bench> --project-directory
/opt/nexaploy/benches/<bench> exec -T backend bench …`, or `run --rm` when it is down); every command
is an argument list and every name is validated against the §2 regexes first.

`site.backup` results carry `{backup_id, path, remote_path, size, created_at, manifest}`;
`image.build` results carry `{tag, built, pushed}`; `backup.prune_remote` results carry
`{site, bench, remote_ids, doomed, pruned:[{ts, remote_path}], kept}`.

## Monitoring facts

`GET /v1/server`, the heartbeat and `server.health` report, besides OS/Docker versions:
`cpu_count`, `load_avg`, `mem_total/mem_available/mem_used`, `mounts` (usage of `/` and
`/opt/nexaploy`), `containers` (Nexaploy containers from `docker ps --all`: shared `nexaploy-*`
plus every bench compose project, with `state` and healthcheck `health`), `bench_health`
(per bench: total, running, unhealthy container names), `unhealthy_containers` (not running or
healthcheck unhealthy; one-shot `configurator` ignored) and `utc_offset_minutes`.

## Usage, site config and backup files

**`site.usage`** `{bench?, site?, ensure_monitor?, refresh_sizes?}` (also `POST /v1/usage`) — step `collect`.
Per site: CPU/request time per UTC day, database size and file storage
(`nexaploy_agent/runtime/usage.py`):

- *CPU*: `site.create` turns on the Frappe monitor (`bench --site s set-config -p monitor 1`,
  step `enable_monitor`; `ensure_monitor: true` does it for older sites). Frappe appends one JSON line
  per request/background job to `benches/<b>/logs/monitor.json.log`; the agent reads it incrementally
  (byte offset + inode per bench in `<root>/agent/data/usage_state.json`; rotation/truncation restarts at 0,
  a trailing partial line waits for the next read) and sums `duration` (µs) of `request` and `job`
  transactions per site per UTC day (7 days kept).
- *Database*: `docker exec -e MYSQL_PWD nexaploy-db mariadb -uroot -N -B -e "SELECT table_schema,
  SUM(data_length+index_length) ..."`; the root password from `db.env` is passed to the docker CLI in the
  environment (`MYSQL_PWD`) only — never argv, step output or `jobs.db`. Matched to `db_name` in site_config.
  Cached 10 min.
- *Storage*: `du -sb` of `sites/<s>/public/files` and `sites/<s>/private/files` (backups excluded),
  cached 1 h, 120 s timeout.

Result: `{collected_at, date, lines_read, sites: {site: {bench, database_mb, storage_mb, monitor_enabled,
days: {YYYY-MM-DD: {cpu_seconds, requests, jobs}}}}, usage}` where `usage` is the compact block below.
With a control plane, the heartbeat carries the same compact block as `usage` **at most every 15 minutes**
(`{collected_at, date, sites: {site: {database_mb, storage_mb, days: {today, yesterday: {cpu_seconds,
requests}}}}}`); if the heartbeat fails it is retried with the next one. Collection errors never break the heartbeat.

**`site.get_config`** `{site}` (also `GET /v1/sites/{s}/config`, synchronous) — result `{config, redacted}`
with `db_password`, `encryption_key`, `admin_password` and any key matching `password|secret|token|key`
masked as `********`.

**`site.update_config`** `{site, set: {key: json}, remove: [keys]}` (also `PATCH /v1/sites/{s}/config`) —
one atomic rewrite of `site_config.json` (Frappe format: `indent=1`, sorted keys, owner 3113, mode kept);
unchanged keys are not rewritten (a no-op job leaves the file alone); `db_*` connection keys,
`encryption_key`, `admin_password`, `root_password` are refused; a key cannot be both set and removed.
Result `{changed, removed}`. Values of sensitive keys are masked in output and scrubbed from `jobs.db`.

**Backup files**: `site.backup` (and the backup step of `site.drop`) results add
`backup_files: [{file_type: database|public|private|site_config, name, size, sha256, remote_key}]`.
`remote_key` is the object key inside the bucket (`<prefix>/<bench>/<site>/<ts>/<name>`; the rclone remote is
an alias of the bucket root) and `null` when the backup was not uploaded. The control plane uses it for
presigned downloads.

## Templates

`nexaploy_agent/templates/` holds copies of the canonical `bench-template/` files
(`bench/docker-compose.yml.j2`, `site-route.yml.j2`, `proxy/`, `db/`). Refresh them with

```bash
agent/scripts/sync_templates.sh            # defaults to ../bench-template
```

(`install.sh` also runs it when installing from a repo checkout). `NEXAPLOY_TEMPLATES` overrides the
directory at runtime.

## Development

```bash
cd agent
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

Tests never touch Docker: every command goes through the `Runner` interface and tests use
`FakeRunner` (records calls, returns scripted results, can emulate side effects).
