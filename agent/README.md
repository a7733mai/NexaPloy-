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

Google Drive / OneDrive / Dropbox (`type=drive|onedrive|dropbox`) use the same layout:
`[<remote>-<type>]` holds the OAuth `token` and `[<remote>]` aliases `<remote>-<type>:`, so paths stay
`<remote>:<prefix>/<bench>/<site>/<ts>`. Switching type removes the other `<remote>-*` sections.
rclone refreshes the access token and rewrites `token` in rclone.conf itself; the agent stores the
sha256 of the token it last wrote in `config/rclone.state.json` (mode 600, outside rclone.conf because
rclone rewrites that file) and, while the control plane keeps sending that same token, keeps the
(refreshed) token on disk. A different token (the customer reconnected) replaces it.

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
| `site.backup` | `site, with_files=true, upload=true, local_retention_days?, pitr=false` | backup, pitr_dump (with `pitr`: replace bench's dump with one carrying binlog coordinates, see below), stage (manifest + sha256), upload (marks the dir `.uploaded`), prune (`local_retention_days`: none = `[backup] retention_days`/`keep_min`; 0 = delete uploaded copies right away; N = uploaded copies older than N days; never-uploaded copies follow the default) |
| `site.restore` | `site, backup_id \| remote_path, bench?, with_files?, restore=all\|database\|files` | locate, fetch, verify, restore (`files`: `bench execute frappe.installer.extract_files` per archive, database untouched, existing site only), restore_config (encryption_key; not for `files`), migrate (not for `files`), write_route |
| `site.migrate` | `site` | migrate |
| `site.set_config` | `site, key, value` | set_config |
| `site.drop` | `site, skip_backup?` | backup, drop, remove_route |
| `server.configure_backups` | `remote, type=s3\|drive\|onedrive\|dropbox, prefix?, retention_days?`; s3: `provider, endpoint?, region?, bucket, access_key_id, secret_access_key`; OAuth types: `token` (rclone token JSON with `access_token` + `refresh_token`), `client_id?`+`client_secret?`, drive: `root_folder_id?` (scope `drive.file`), onedrive: `drive_id, drive_type, tenant?` | write_rclone_conf, update_agent_config |
| `backup.test_remote` (`POST /v1/server/backup-test`) | `destination?` (configure_backups params, tested from a throw-away rclone config), `prefix?` | check (`rclone mkdir`, `touch` + `deletefile` a marker, `lsf`, `about --json` quota when supported) |
| `server.health` | — | collect (result = host facts incl. containers / bench health) |
| `backup.prune_remote` | `site, bench, keep_days=14, keep_min=3, keep_daily=0, keep_weekly=0, keep_monthly=0, prefix?` | list (`rclone lsf --dirs-only`, select dated dirs older than keep_days, newest keep_min kept, plus the newest of each of the last N days / ISO weeks / months), prune (`rclone purge` each) |

### Site lifecycle (`jobs/lifecycle_jobs.py`, `runtime/lifecycle.py`)

| Kind | Params | Steps |
|---|---|---|
| `site.clone` (`POST /v1/sites/{s}/clone`) | `bench, site, target_site, scrub=true, target_bench?` (copy on another bench of this server: backup on `bench`, everything else on `target_bench`) | check, backup, stage (local backup of `site`, never uploaded), new_site (skipped when `target_site` exists; marks it `nexaploy_staging_of: <site>` in its site_config), restore (`bench --site <target> restore … --force`), restore_config (source `encryption_key`), scrub (`mute_emails=1`, `pause_scheduler=1`, `maintenance_mode=0`, no `host_name`/`domains`; ``UPDATE `tabEmail Account` SET enable_outgoing=0, enable_incoming=0`` via `docker exec -e MYSQL_PWD nexaploy-db mariadb`), migrate, write_route |
| `site.rename` (`POST /v1/sites/{s}/rename`) | `bench, site, new_name, keep_old_domain=true` | check, maintenance_on, move (`sites/<old>` → `sites/<new>` on the host bind mount; skipped when already moved), update_config (`host_name` rewritten, new name removed from / old name added to `domains`), write_route (old route removed), clear_cache, maintenance_off (back to the previous value) |

`site.clone` refuses to overwrite an existing site unless it carries the staging marker of the same
source (re-running it refreshes a staging copy). Database name and files stay the same on rename.
Moves between servers need no extra kind: `site.backup` → `site.restore {bench, remote_path}` on the
target (restore creates the site) → `site.drop` on the source.

### Point-in-time recovery (`jobs/pitr_jobs.py`, `runtime/pitr.py`, ARCHITECTURE.md §14)

| Kind | Params | Steps |
|---|---|---|
| `server.archive_binlogs` (`POST /v1/server/archive-binlogs`) | `rotate=true, keep_days=7, prefix?` | status (`SELECT @@log_bin, @@binlog_format`; off/not ROW → success with `enabled: false`), rotate (`FLUSH BINARY LOGS`), upload (closed files of `db/data/mysql-bin.index` → `rclone copy --immutable --files-from` to `<remote>:<prefix>/_binlogs/`), prune (`rclone delete --min-age <keep_days>d`) |
| `site.restore_pitr` (`POST /v1/sites/{s}/restore-pitr`) | `site, backup_id \| remote_path, target_time (UTC ISO), target_site?, bench?, in_place=false, confirm?, with_files=true, scrub=true, binlog_prefix?` | new copy: check, locate, fetch, verify (manifest `binlog`, `ready_at <= target`), collect_binlogs, ensure_site, restore, restore_config, scrub, sync_files, decode, apply, migrate, write_route, cleanup. In place (`confirm` = site name): check, locate, fetch, verify, collect_binlogs, safety_backup, maintenance_on, restore, restore_config, decode, apply, migrate, maintenance_off, write_route, cleanup |

- `pitr_dump` runs `mariadb-dump --single-transaction --master-data=2 --gtid` (one-shot `docker run` of the
  bench image, so `bench restore` reads it like bench's own dump) into a temp file renamed over bench's
  database dump, then parses `-- CHANGE MASTER TO MASTER_LOG_FILE=…, MASTER_LOG_POS=…` / `gtid_slave_pos`
  from the header. Result and manifest: `binlog: {file, position, gtid, db_name, ready_at, server_id}`;
  `ready_at` (dump finished) is the earliest restorable time. Binlog off or dump failure: the backup still
  succeeds with `binlog: null` and `pitr_error`.
- `collect_binlogs` flushes the binlog (every event before the target is then in a closed file), lists
  `db/data/mysql-bin.index` and the archive (`rclone lsjson`), selects the consecutive files from the
  backup's file (a gap fails the job before anything is touched) and copies / fetches them to
  `backups/.pitr/<job>/binlogs` (uid 3101).
- `decode` runs `mariadb-binlog --disable-log-bin --start-position=<pos> --stop-datetime=<target>
  [--rewrite-db=<src>-><dst>] --database=<dst>` in a one-shot container of the DB server's image (`TZ=UTC`)
  into `work/replay.sql` and reports `{bytes, row_events, commits}`; `apply` pipes it into
  `mariadb --binary-mode` as root. `apply` is the one step that cannot run twice: an interrupted apply
  restores the base backup again before replaying.
- The copy (default `<first label>-pitr-<YYYYmmddHHMM>.<rest>`) is marked `nexaploy_pitr_of` /
  `nexaploy_pitr_target` with `mute_emails=1, pause_scheduler=1` (scrub); files uploaded after the backup
  are copied from the live site (`sync_files`, nothing overwritten).

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

## Agent upgrades (`jobs/agent_jobs.py`, `runtime/upgrade.py`)

The control plane sends `agent.upgrade {version, ref, repo_url}` ("Upgrade agent" on a Server).
The new version is built next to the running one (`/opt/nexaploy/agent/.venv.new`), self-checked
with `nexaploy-agent version`, swapped in (the old venv stays at `.venv.prev`), and both units
restart 10 s after the job reported success. A watchdog (systemd-run, from `.venv.prev`) rolls
back automatically when the new agent does not answer `/v1/ping` with the new version.

```bash
nexaploy-agent upgrade --version 0.2.0          # standalone servers: queue the job locally
nexaploy-agent rollback                         # put .venv.prev back and restart the units
# if even the CLI of the new venv is broken:
/opt/nexaploy/agent/.venv.prev/bin/python -m nexaploy_agent.agent_cli rollback
```

Benches and sites are never touched; proxy/DB stack changes still need `install.sh`.

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

## Observability jobs (ARCHITECTURE.md §10)

`nexaploy_agent/jobs/observability_jobs.py`, runtime in `runtime/logs.py`, `runtime/sql.py`,
`runtime/console.py`. Results are returned in the job result (capped so `job_update` stays small); bulk
command output is not duplicated into step output.

| Kind / route | Params | Result |
|---|---|---|
| `site.logs` — `GET /v1/sites/{s}/logs?log=&lines=&since=` | `site, log: web\|worker\|scheduler\|frappe\|error, lines (≤5000), since? (30m, 2h, 2026-11-03T10:00Z)` | `{sources: [{source, kind: file\|container, lines, truncated, filtered}], shared_bench, truncated}`; `error`: `{rows: [{creation, method, error}]}` |
| `site.analytics` — `GET /v1/sites/{s}/analytics?date_from=&date_to=` | `site, date_from?, date_to? (YYYY-MM-DD, ≤31 days, default last 7), top=20` | `{requests: {count, total_ms, avg_ms, p50_ms, p95_ms}, jobs, paths_by_count, paths_by_duration, jobs_by_method, status_codes, hourly, daily, monitor_enabled}` |
| `site.slow_queries` — `GET /v1/sites/{s}/slow-queries?limit=&min_seconds=` | `site, limit=50, min_seconds=0` | `{enabled, log_size, entries, queries: [{query, count, avg_time, max_time, total_time, rows_examined_avg/max, rows_sent_avg, example, last_seen}]}` |
| `site.sql` — `POST /v1/sites/{s}/sql` | `site, query, read_only=true, max_rows=500 (≤5000), timeout=30 (≤300)` | `{columns, rows, truncated, row_count, result_sets: [...], statements, read_only, error}` |
| `site.console` — `POST /v1/sites/{s}/console` | `site, code, commit=false, timeout=60 (≤600), allow_shared_bench=false` | `{ok, output, truncated, committed, error, duration}` |
| `site.smoke_check` — `POST /v1/sites/{s}/smoke-check` | `site, paths=["/api/method/ping", "/login"] (≤10), timeout=20 (≤120), apps=[]` | `{checks: [{name, ok, detail}], http: [...], ok, summary}`; steps http (GET `http://127.0.0.1:8000<path>` with `Host: <site>` inside the backend container, 200 required, ping must say `pong`), database (`frappe.get_all("User", limit=1)`, every app in `apps` installed), verdict (fails the job listing the failed checks) |

The GET routes enqueue a job like every other route (`202 {job_id}`); jobs still run serially on the worker.

- **Logs**: bench files `logs/{web,worker,scheduler}[.error].log`, `logs/frappe.log`, site files
  `sites/<s>/logs/{frappe,database,scheduler}.log`, plus `docker compose logs --no-color --timestamps --tail N
  [--since S] <service>` (web → backend, worker → queue-short/default/long, scheduler → scheduler). When the
  bench hosts several sites, bench-level and container lines are **filtered to lines mentioning the site**.
  2 MB total. `log=error` reads the newest `tabError Log` rows (≤200, error ≤4000 chars) with a read-only query.
- **Analytics** aggregates `logs/monitor.json.log*` (the Frappe monitor, see usage) for the site and range.
- **Slow queries** parse the tail (64 MB) of `<root>/db/data/slow.log` (enabled by
  `--slow-query-log=1 --slow-query-log-file=/var/lib/mysql/slow.log --long-query-time=${DB_LONG_QUERY_TIME:-1}`
  in the DB stack — restart `nexaploy-db` after upgrading), keep entries whose `Schema:` (or `use db`) is the
  site's `db_name`, normalise literals to `?` and group. The log is never rotated by the agent.
- **SQL** runs `docker exec -i -e MYSQL_PWD nexaploy-db mariadb --sandbox --xml --user <site db user> --host
  127.0.0.1 --protocol TCP --database <db_name>` with the script on stdin: `SET SESSION max_statement_time`,
  `sql_select_limit = max_rows+1`, and in read-only mode `SET SESSION TRANSACTION READ ONLY; START TRANSACTION;
  …; ROLLBACK;`. The site's password (site_config `db_password`) is only in the environment. Read-only accepts
  SELECT/SHOW/DESCRIBE/DESC/EXPLAIN/WITH; every mode rejects mariadb client commands (`system`, `source`, `use`,
  `delimiter`, …), backslashes outside literals and `/*! */` comments. `--xml` (instead of `--batch --raw`)
  keeps several result sets, NULLs and values with tabs/newlines apart. `--sandbox` needs MariaDB ≥ 10.6.18.
  An SQL error is returned as `result.error` (job success).
- **Console** writes the code to `sites/.nexaploy-console/<job>.py` (dir 700, file 600, uid 3113; removed after)
  and runs `docker compose … exec -T -w /home/frappe/frappe-bench/sites backend env/bin/python -c <bootstrap>`:
  `frappe.init(site, sites_path=".")`, `frappe.connect()`, `exec(code)` with stdout/stderr captured (1 MB),
  `frappe.db.commit()` if `commit` else `rollback()`, `frappe.destroy()`; `signal.alarm(timeout)` inside the
  container. Refused when the bench is down or (without `allow_shared_bench`) hosts other sites.
- `site.sql` in write mode and `site.console` are not re-run when a crash interrupted them (the resumed step fails).

## TLS status (custom domain health)

**`site.tls_status`** `{site?, domains?}` (also `POST /v1/tls-status`) — step `check`
(`nexaploy_agent/runtime/tls.py`). For every routed host (all sites, one site, or the listed domains) the
agent connects to Traefik on `127.0.0.1:<HTTPS_PORT>` (`proxy/.env`, default 443) with SNI = host, first
verified (system CA store), then — when verification fails — unverified to still describe the certificate
(decoded with CPython's own DER decoder, no extra dependency). Per host: `trusted`, `not_before`,
`not_after` (ISO UTC), `issuer`, `subject_cn`, `sans`, `default_cert` (Traefik's `TRAEFIK DEFAULT CERT`,
i.e. ACME has not issued one), `verify_error`, `error`. Probes run 8 in parallel with a 5 s timeout.
The heartbeat carries the same report as `tls` at most every 10 minutes, and `facts.public_ips` lists the
global source addresses of the default IPv4/IPv6 routes (DNS targets for custom domains).

Routes: one Traefik router per host (`site-<slug>` for the primary name, `site-<slug>--<host slug>` per custom
domain), so each host gets its own certificate and a broken custom domain cannot block the others.
### Managed e-mail relay

**`site.configure_email`** — `POST /v1/sites/{s}/email` — `{site, enabled=true, account_name="Nexaploy Relay",
email_id, smtp_server, smtp_port=587, tls_mode: starttls|ssl|none, username?, password?, verify_smtp=true}` →
`{ok, account, email_id, created, changed, enabled}`. Runs a fixed script (`runtime/mail_relay.py`) with
`docker compose … exec -T -w /home/frappe/frappe-bench/sites backend env/bin/python -c <bootstrap> <site>`; the
settings JSON, password included, goes to **stdin** only. It creates/updates the site's default outgoing Email
Account (only fields present in the site's Frappe version), saves only on change, and with `verify_smtp=false`
saves under `flags.in_patch` (no SMTP login check). `enabled=false` turns outgoing off and keeps the account.
Allowed on shared benches (no user code). Fails when the bench is down.
