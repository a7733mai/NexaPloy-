# nexaploy — control plane (component D)

Frappe app (v15+) that manages Nexaploy servers, release groups, benches, sites, backups,
the app marketplace and plans. It talks to servers **only** through the agent protocol in
[`ARCHITECTURE.md` §5.6](../ARCHITECTURE.md#56-control-plane-protocol): agents poll for work;
the control plane never connects to a server.

## Install

```bash
cd ~/frappe-bench
bench get-app /path/to/NexaPloy-/control-plane      # or the git URL
bench --site cp.example.com install-app nexaploy
bench --site cp.example.com migrate
bench --site cp.example.com enable-scheduler
```

`after_install` / `after_migrate` (`nexaploy/install.py`) create the **Nexaploy Customer**
role (also shipped as a fixture) and the default plans:

| Plan | Type | Price |
|---|---|---|
| BYOS Server | Server BYOS | $10 / server / month |
| Hosted Small | Server Hosted | $40 / month |
| Site Basic | Site | $5 / month |

### Serving `install.sh`

The installer lives in the agent component: `agent/install/install.sh`. The control plane
does not ship a route for it; publish it at `https://<control-plane>/install.sh` from the web
server in front of Frappe, e.g. nginx:

```nginx
location = /install.sh { alias /srv/nexaploy/agent/install/install.sh; default_type text/x-shellscript; }
```

## DocType map (module "Nexaploy")

| DocType | Naming | Notes |
|---|---|---|
| Server | `server_id` (`srv-xxxxxxxx`) | `ownership` Hosted/BYOS, `status` Pending/Active/Offline/Archived, `token_hash` (sha256 only), `last_heartbeat`, `facts`, `plan`, `is_build_server`. Methods: `rotate_token`, `archive`. |
| Server Registration Token | hash | single use, `expires_on`, `used`, `server`. `generate_registration_token(team)` |
| Marketplace App | `app_name` | `approved`, `pricing` Free/Paid, child **Marketplace App Release** (`frappe_major`, `branch`, `commit`) |
| Release Group | `title` (slug, used in image tags) | `frappe_major`, `frappe_branch`, `team`, `build_server`, child **Release Group App** (`app`, `branch`). Methods: `build_apps_json`, `create_deploy_candidate` |
| Deploy Candidate | hash | `build_id`, `image_tag` = `nexaploy/bench:<group>-<build_id>`, `apps_json`, `status` Pending/Building/Success/Failure, `build_log` |
| Bench | `bench_name` | `server`, `release_group`, `deploy_candidate`, `image_tag`, `status` Pending/Active/Broken/Archived. Methods: `start`, `stop`, `restart`, `update_image`, `remove` |
| Site | `site_name` (FQDN, §2) | `bench`, `server` (fetched), `team`, `plan`, `admin_password` (Password), children **Site App**, **Site Domain**; `status` Pending/Active/Moving/Broken/Suspended/Archived |
| Site Backup | hash | `remote_path`, `with_files`, `purpose` Manual/Scheduled/Move, `status`, `size`, `created_at` |
| Agent Job | hash (sent to the agent as `job_id`) | `server`, `kind`, `params` (JSON), `status` Pending/Queued/Running/Success/Failure, `reference_doctype/name`, `error`, `result`, child **Agent Job Step** (`step_idx`, `step_name`, `status`, `output`, `started_at`, `ended_at`) |
| Nexaploy Plan | `title` | `plan_type` Server Hosted/Server BYOS/Site, `price_monthly`, `currency`, `max_sites` |
| Nexaploy Settings | single | registry (`registry_url`, `registry_username`, `registry_password`), `root_domain` (`nexaploy.cloud`), `default_build_server`, `alert_email`, backup storage (`backup_remote_type` s3, `backup_provider`, `backup_endpoint`, `backup_region`, `backup_bucket`, `backup_prefix`, `backup_access_key`, `backup_secret_key`) |
| Server Metric | hash | `server`, `timestamp`, `load_1`, `memory_used_mb/total_mb`, `disk_used_gb/total_gb`, `unhealthy_containers`, `raw` |

Operations fields: **Server** `cpu_count`, `memory_total_mb`, `disk_total_gb`, `disk_used_percent`,
`unhealthy_containers`, `last_alert_at` (+ hidden `alert_state`), methods `health_check`,
`configure_backups`; **Site** `backup_frequency` (Daily/Every 12 hours/Weekly/Off),
`backup_retention_days` (14, 0 = forever), `backup_with_files`, `last_backup_at`, `is_up`,
`last_uptime_check`, hidden `suspended_for_billing` (set by billing only); **Site Backup** status
`Deleted`; **Release Group** `auto_update`, `update_window`, method `deploy_latest`.

`team` is a Link to **Team** on every DocType (see *Teams* below). Users only see rows of
teams they are members of (`nexaploy/permissions.py`).

## Code map

| Path | What |
|---|---|
| `nexaploy/api/agent.py` | `register`, `heartbeat`, `poll`, `job_update` (§5.6) + `validate_agent_auth` auth hook |
| `nexaploy/api/marketplace.py` | `list_apps(frappe_major=None)` (guest-readable) |
| `nexaploy/teams.py` | team membership — the only place it is checked: `get_current_team`, `check_team_access`, `get_user_teams`, `get_team_role`, `is_platform_admin`, `team_notification_emails` |
| `nexaploy/team_utils.py` | pure team logic (no frappe import): role matrix, permission-type mapping, slugs, invitation tokens, last-Owner rules, notification recipients |
| `nexaploy/api/teams.py` | portal team API: `my_teams`, `switch_team`, `create_team`, `get_team`, `update_team`, `invite_member`, `get_invitation`, `accept_invitation`, `revoke_invitation`, `remove_member`, `change_role`, `leave_team` |
| `nexaploy/agent_job.py` | `create_agent_job(server, kind, params, reference_doctype, reference_name)` |
| `nexaploy/agent_callbacks.py` | per-kind callbacks run by `job_update` |
| `nexaploy/utils.py` | pure logic (no frappe import): name regexes, token hashing, status mapping, compatibility, apps.json, move plan |
| `nexaploy/tasks.py` | scheduler: mark servers Offline after 5 min without heartbeat (every minute), prune expired unused registration tokens (hourly), operations tasks (see *Operations*) |
| `nexaploy/ops.py` | operations glue: registry params, backup config push, heartbeat metrics, alert emails, auto updates |
| `nexaploy/ops_utils.py` | pure operations logic (no frappe import): registry tags, backup due, retention, metrics, alert rate limit, update windows |
| `tests/` | plain pytest, runs without a bench: `python -m pytest control-plane/tests` |

## Teams

A **Team** (ARCHITECTURE.md §9) owns servers, release groups, benches, sites, backups,
subscriptions and invoices; every `team` field is a Link to Team.

| DocType | Naming | Notes |
|---|---|---|
| Team | `team_name` (slug of the title, unique; `acme-corp`, `acme-corp-2`, …) | `title`, `owner_user`, `billing_email`, `currency`, `enabled`, `two_factor_required`, child **Team Member** (`user`, `role` Owner/Admin/Developer/Billing) |
| Team Invitation | hash | `team`, `email`, `role`, `token_hash` (sha256 only), `expires_on` (7 days), `status` Pending/Accepted/Expired/Revoked, `invited_by`, `accepted_by`, `accepted_on` |

| Action | Owner | Admin | Developer | Billing |
|---|---|---|---|---|
| `read` | ✅ | ✅ | ✅ | ✅ |
| `write` (sites, benches, apps, backups, domains, config) | ✅ | ✅ | ✅ | ❌ |
| `billing` (invoices, payments, plans) | ✅ | ✅ | ❌ | ✅ |
| `admin` (members, invitations, servers, registration tokens) | ✅ | ✅ | ❌ | ❌ |

- **Current team**: header `X-Nexaploy-Team`, else cookie `nexaploy_team`, else the user's first
  team. Values the user is not a member of are ignored. A user without any team gets a personal
  team (`<user>'s team`, Owner) on first use. Platform admins (System Manager /
  Administrator) pass every check and may select any team.
- **Row permissions** (`permissions.py`): list queries use
  `team in (select parent from tabTeam Member where parenttype='Team' and user=…)` (enabled teams);
  `has_team_permission` maps Frappe ptypes to actions (read/select/print/export → `read`;
  write/create/submit/cancel/delete → `write`, `billing` on invoices/payments/subscriptions,
  `admin` on servers, registration tokens, teams and invitations).
- **Portal**: sidebar team switcher (`switch_team` sets the cookie; the JS sends
  `X-Nexaploy-Team` on every call), `/dashboard/team` (members, roles, invitations,
  `?invite=<token>` accepts). `overview`, `get_site`, `get_server` return `role` and
  `permissions` and `NX_BOOT.role` drives `nx.can(action)` to hide write/billing/admin buttons.
- **Invitations**: `invite_member` (admin) emails `/dashboard/team?invite=<token>`; only an Owner
  may invite/grant/revoke/remove Owners; a team always keeps one Owner. Accepting requires the
  invitee's email, adds the member and grants the *Nexaploy Customer* role.
- **Notifications**: `team_notification_emails(team, "ops")` → Owner/Admin/Developer (server
  alerts, uptime); `"billing"` → `billing_email` + Owner/Billing members (payment checkout email).
- **Migration**: patch `nexaploy.patches.v0_2.user_team_to_team` creates a personal team for each
  User found in a `team` field and rewrites those fields to the Team name.

## Agent protocol

Agents send `Authorization: token <server_token>` and their `server_id` (query string for
`poll`, JSON body otherwise). The server is looked up by `server_id` and
`sha256(token)` is compared to `Server.token_hash` with `hmac.compare_digest`.

Frappe v15 rejects any two-part `Authorization` header that did not log in a user *before*
the method runs, so `hooks.auth_hooks` registers `validate_agent_auth`: for the four
`nexaploy.api.agent.*` paths only, it verifies the agent token and runs the request as
Administrator; the endpoint verifies the token again. Tokens never contain `:` so Frappe's
own `token key:secret` parser ignores them.

| Endpoint | Behaviour |
|---|---|
| `register` (POST) | `{registration_token, hostname, facts, agent_version}` → consumes the token (sha256 lookup, single use, not expired) → creates Server (BYOS, Active, BYOS plan, team from token) → `{server_id, server_token}` (plaintext shown once) |
| `heartbeat` (POST) | stores `facts`, `benches`, `agent_version`, `last_heartbeat`; Offline → Active |
| `poll` (GET) | up to 20 Pending jobs of this server, oldest first, → Queued; returns `[{job_id, kind, params}]` with secret placeholders resolved |
| `job_update` (POST) | maps `queued/running/success/failure(/cancelled)` → Queued/Running/Success/Failure (forward-only, terminal sticky), upserts steps by `idx`, stores `error`/`result`, and on a status change runs the callback for `kind` (once per terminal status) |

### Job params (what the control plane sends)

Params are the agent REST body (§5.2) plus the path parameters of that endpoint:

| kind | params | created by |
|---|---|---|
| `image.build` | `{major, apps_json:[{url,branch}], tag, frappe_branch[, push: true, registry]}` | Deploy Candidate insert (on Release Group `build_server`, else Settings `default_build_server`) |
| `bench.create` | `{name, image, apps_json[, registry]}` | Bench insert |
| `bench.start/stop/restart/remove` | `{bench[, force]}` | Bench methods |
| `bench.update` | `{bench, image[, registry]}` | `Bench.update_image`, auto updates |
| `site.create` | `{bench, name, admin_password, apps:[app_name]}` | Site insert |
| `site.install_app` / `site.uninstall_app` | `{site, app}` | Site methods |
| `site.add_domain` / `site.remove_domain` | `{site, domain}` | Site methods |
| `site.backup` | `{site, with_files, upload: true}` | Site Backup insert |
| `site.restore` | `{site, remote_path}` (restore) or `{site, bench, backup_id\|remote_path, with_files}` (move target) | restore, move |
| `site.migrate` | `{site}` | `Site.migrate` |
| `site.drop` | `{site, skip_backup}` | `Site.drop`, move |
| `server.configure_backups` | `{remote, type, provider, endpoint, region, bucket, access_key_id, secret_access_key, prefix}` | Server becomes Active (register / first heartbeat), Settings backup change, `Server.configure_backups` |
| `server.health` | `{}` | `Server.health_check` |
| `backup.prune_remote` | `{site, bench, prefix, keep_days, keep_min: 3}` | `tasks.prune_remote_backups` (daily) |

`registry` is `{url, username, password}` and is only sent when the image lives on the configured
registry. `registry.password` and `secret_access_key` are stored as secret placeholders
(`__secret__:Nexaploy Settings:Nexaploy Settings:<field>`) and resolved only in `poll`.

`admin_password` is stored in Agent Job params as a placeholder
(`__secret__:Site:<site>:admin_password`) and replaced with the decrypted Password field only
in the `poll` response, so plaintext secrets never land in the Agent Job table.

### Callbacks

| kind | Success | Failure |
|---|---|---|
| `image.build` | Deploy Candidate Success (+ `image_tag` from `result.tag`, `build_log` from steps); Running → Building | Failure + log |
| `bench.create/update/start/restart` | Bench Active (`bench.update` sets the new image) | Broken |
| `bench.remove` | Bench Archived | — |
| `site.create` | Site Active, pending apps Installed | Broken, apps Failed |
| `site.install_app` / `uninstall_app` | row Installed / removed | row Failed |
| `site.add_domain` / `remove_domain` | domain Active / removed | domain Broken |
| `site.backup` | Site Backup Success, `remote_path`/`size`/`created_at` from `result` | Failure |
| `site.restore`, `site.migrate` | Site Active | Broken |
| `site.drop` | Site Archived | — |
| `image.build` (auto update) | if the group has `auto_update`: `bench.update` for Active benches inside `update_window` | — |
| `bench.update` | also `deploy_candidate` = the build with that `image_tag` | — |
| `site.backup` | also `Site.last_backup_at` | — |
| `backup.prune_remote` | Site Backups whose `remote_path` was purged → Deleted | error log |
| `server.health` | Server `facts` + monitoring fields / Server Metric | — |
| `server.configure_backups` | — | error log |

Site app and drop callbacks and `Server.archive` call `nexaploy.billing.subscriptions.sync_subscription`
(errors are logged, never raised).

## Flows

### BYOS server

1. Customer (or staff) calls
   `nexaploy.nexaploy.doctype.server_registration_token.server_registration_token.generate_registration_token`
   → `{token, expires_on, install_command}`; default TTL 24 h. The token is stored as sha256.
2. Customer runs on a fresh Ubuntu box:
   `curl -fsSL https://cp.example.com/install.sh | sudo bash -s -- --token <T> --control-plane https://cp.example.com`
3. `install.sh` calls `register`; the Server appears as **Active / BYOS** on the *BYOS Server*
   plan with the token's team. The agent then heartbeats and polls.
4. Uninstalling the agent leaves benches and sites running (no lock-in).

Hosted servers: create the Server manually (ownership Hosted), call `rotate_token` and install
with `--server-id <id> --token <server_token>`.

### Image → bench → site

1. Create Marketplace Apps (approved, with releases per major).
2. Create a Release Group (`frappe_major`, apps, `build_server`). `validate` rejects apps that are
   not approved or have no release for the major; missing branches default to the release branch.
3. `create_deploy_candidate` → `image.build` on the build server → candidate Success.
4. Create a Bench on a server (uses the latest successful candidate) → `bench.create`.
5. Create a Site on an Active bench with apps from the release group → `site.create`.

### Moving a site (`Site.move_to_bench(target_bench, allow_upgrade=False)`)

Never in place (§1); also how a site changes server or Frappe major (see *Site lifecycle* below for
the portal API). The plan (`lifecycle_utils.plan_move`) is stored on the Site (`move_steps`,
`move_step_idx`, `move_backup`, `move_target_bench`, hidden `move_options`; status **Moving**) and each
successful step's callback enqueues the next one:

- different server: `site.backup` (uploaded) → `site.restore {bench, remote_path}` (target: creates the site,
  restores files + encryption key, migrates, writes the route) → `site.drop {skip_backup}` (source)
- same server: `site.backup` → `site.drop` (source) → `site.restore {bench, backup_id}` (local backup)
  (the agent resolves a site's bench by scanning `benches/*/sites/<s>`, so a name can only live in one bench per server)

A failure before the source is dropped aborts the move (site stays **Active** where it was, a comment
explains why); a later failure marks the site **Broken** with the move backup linked in `move_backup`.
After success `bench`/`server` are updated and the custom domains are re-added on the target
(`site.add_domain`). Moves planned before this change (with a `site.create` step) still complete.
Cross-server moves need backup storage (Nexaploy Settings `backup_bucket`); a higher Frappe major needs
`allow_upgrade`, a lower one is refused.

## Operations

**Registry.** Set `Nexaploy Settings.registry_url` (e.g. `ghcr.io/org`, `registry.example.com`) and
credentials: new builds are tagged `<registry_url>/nexaploy/bench:<group>-<build_id>`, the build
server logs in (`docker login --password-stdin`) and pushes, and `bench.create`/`bench.update` on any
server log in before pulling. Without a registry, images stay local to the build server.

**Backup storage.** Fill the backup section of Nexaploy Settings (S3-compatible: endpoint, region,
bucket, keys). On save, a fingerprint of the fields (incl. the secret) is compared with the last
pushed one and, if different, `server.configure_backups` is queued for every Active/Offline server;
newly Active servers get it too. Each server writes to `<backup_prefix>/<server_id>/<bench>/<site>/<ts>`
in the shared bucket (rclone remote `nexaploy-backups`), so moves across servers can restore.

**Scheduled backups & retention.** `schedule_backups` (hourly) creates a *Scheduled* Site Backup for
each Active site on an Active server when `backup_frequency` has elapsed since `last_backup_at`
(10 min slack) and no backup is Pending/Running. `prune_remote_backups` (daily) queues
`backup.prune_remote` for each `(prefix, bench)` the site's backups live in, keeping
`backup_retention_days` and always the newest 3; purged Site Backups become *Deleted*. Archived
sites and retention 0 are never pruned.

**Monitoring.** Each heartbeat updates the Server monitoring fields and inserts a Server Metric at
most every 5 minutes (pruned after 30 days by `prune_server_metrics`). `check_alerts` (every 5 min)
emails the server's team and `alert_email` for disk > 85 %, Offline and unhealthy containers, at
most once per 6 h per server and kind. `check_sites_uptime` (every 5 min) requests
`https://<site>/api/method/ping` (10 s timeout, 16 in parallel) for Active sites, stores `is_up` /
`last_uptime_check` and emails on down/up transitions.

**Auto updates.** `Release Group.deploy_latest()` builds a new image. When it succeeds and the
group has `auto_update`, Active benches not on that image get `bench.update` if their server's local
time (agent `utc_offset_minutes`) is within `update_window` (`HH:MM-HH:MM`, may wrap midnight, empty =
any time); `apply_auto_updates` (every 15 min) catches benches outside the window later.

Scheduler entries (hooks.py):

```python
scheduler_events = {
	"cron": {
		"* * * * *": ["nexaploy.tasks.mark_offline_servers"],
		"*/5 * * * *": ["nexaploy.tasks.check_alerts", "nexaploy.tasks.check_sites_uptime"],
		"*/15 * * * *": ["nexaploy.tasks.apply_auto_updates"],
	},
	"hourly": ["nexaploy.tasks.prune_registration_tokens", "nexaploy.tasks.schedule_backups"],
	"daily": ["nexaploy.tasks.prune_remote_backups", "nexaploy.tasks.prune_server_metrics"],
}
```

## Plan limits & usage

**Plans.** Site plans (Nexaploy Plan, `plan_type` Site) carry `cpu_seconds_per_day`, `max_database_mb`,
`max_storage_mb` (0 = unlimited), `overage_action` (Notify / Throttle / Suspend) and `plan_order` (upgrade
suggestions go up in this order, then price).

**Collection.** Agents report per-site usage with the heartbeat at most every 15 minutes (`usage` block,
ARCHITECTURE §5.6) and as the result of `site.usage` jobs. `nexaploy.ops.ingest_usage(server, usage)` upserts
one **Site Usage** row per site per UTC day (`cpu_seconds`, `requests`, `database_mb`, `storage_mb`; CPU never
decreases within a day) — only for sites on that server — and updates Site `current_cpu_seconds_today`,
`database_mb`, `storage_mb`, `usage_updated_at`. It never raises. The heartbeat endpoint calls it with
`ops.ingest_usage(server, kwargs.get("usage"))`.

**Enforcement** (`tasks.enforce_plan_limits`, every 15 min; `ops.enforce_site_limits(site)`). Pure decisions in
`ops_utils.usage_status(plan, usage)` (ok / warn ≥ 80 % / over > 100 % per metric) and
`ops_utils.enforcement_actions(plan, status, site)`:

| overage_action | warn | over | back under (or next UTC day for CPU) |
|---|---|---|---|
| Notify | email | email | — |
| Throttle | email | email + site_config `rate_limit {limit: max(60, 10 % of the CPU budget), window: 86400}` (Frappe's request-time rate limiter) when **CPU** is over; Site `throttled` | `rate_limit` removed, `throttled` = 0 |
| Suspend | email | email + Site **Suspended** (`usage_suspended`, not `suspended_for_billing`) + `maintenance_mode: 1` when any metric is over | `maintenance_mode: 0`, Site Active |

Emails go to `nexaploy.teams.team_notification_emails(team, "ops")` (fallback: ops alert recipients), at most
once per 24 h per level (`Site.usage_alert_state`). Config changes go out as one `site.update_config` job.
Billing-suspended sites are never touched. `Site.over_limit` mirrors the current state. Site Usage rows are
pruned after 400 days (`tasks.prune_site_usage`, daily).

## Backup downloads

`site.backup` callbacks store a **Site Backup File** row per file (`file_type` database / public / private /
site_config, `file_name`, `remote_key`, `size`, `sha256`) from the agent's `backup_files`.
`nexaploy.api.site_tools.download_backup(backup, file_type)` (team `read`) returns `{url, expires_in: 900}`: an
AWS Signature V4 query-string presigned GET (`ops_utils.presign_s3_get`, pure, verified against the AWS
documented example) built from the Nexaploy Settings backup credentials. No endpoint = AWS virtual-hosted
style; a custom endpoint (MinIO, Wasabi, R2, ...) = path style `<endpoint>/<bucket>/<key>`. Each download is
logged as a comment on the Site Backup. Backups taken under a different bucket than the current settings
cannot be downloaded this way.

## Site config editor

`nexaploy.api.site_tools` (team `write`): `get_site_config(site, refresh=0)` queues `site.get_config` (result
under a minute old is reused) → poll `get_site_config_result(site, job)`; secrets are redacted by the agent and
again by `ops_utils.redact_site_config` (which also lists read-only keys). `update_site_config(site, changes,
remove)` validates with `ops_utils.validate_config_changes` — JSON-typed values (no null; use `remove`), ≤ 16 KB
each, ≤ 50 keys, the mask `********` is refused, and the deny-list `CONFIG_DENY` (`db_*`, `encryption_key`,
`host_name`, `domains`, `rate_limit`, `monitor`, `limits`, redis/ports, `nexaploy_*`) — returns warnings for
`developer_mode`, `maintenance_mode`, `pause_scheduler`, ... and queues one `site.update_config`. Secret values
are scrubbed from Agent Job.params when the job finishes.

`change_plan(site, plan)` (team `billing`) saves Site.plan (the Site `on_update` hook re-syncs the billing
subscription) and re-runs enforcement immediately. `get_usage(site)` returns today's usage vs the plan,
30-day history and the plans to upgrade to. Portal: `/dashboard/site` tabs *Usage* and *Config* and download
buttons on backups (`public/js/site_tools.js`, Alpine component `nxSiteTools`).

Scheduler additions (hooks.py):

```python
"*/15 * * * *": [..., "nexaploy.tasks.enforce_plan_limits"],
"daily": [..., "nexaploy.tasks.prune_site_usage"],
```

## Billing notes (Phase 3, not implemented)

- Plans exist as data only; nothing is invoiced yet. BYOS = flat management fee per server
  ($10/month, the customer pays their VM provider). Hosted = server plan. Sites on shared
  benches = site plan; `max_sites` caps sites per plan (0 = unlimited) — not enforced yet.
- Paid marketplace apps carry `price_monthly`; billing per installed Site App is a Phase 4 item.
- Usage source of truth: Server (plan, status ≠ Archived) and Site (plan, status Active/Broken).

## Tests

```bash
python -m pytest control-plane/tests
```

`tests/test_utils.py` covers the pure logic; `tests/test_doctypes.py` validates every DocType
JSON (required keys, unique fieldnames, child tables exist and are `istable`, links resolve,
§8 fields present), the app layout, and that every `hooks.py` target exists. Controller and API
code needs a real bench (`bench --site test.local run-tests --app nexaploy`). Not yet verified
on a bench:

- `bench get-app` / `install-app` / DocType sync of the generated JSON;
- the `auth_hooks` workaround for `Authorization: token <T>` on Frappe v15/v16;
- `for_update` row locking in `poll` / `job_update` / `register`;
- callbacks and the `move_to_bench` chain against a real agent;
- permission query conditions for the *Nexaploy Customer* role (team membership subqueries);
- team switcher cookie/header handling and invitation emails.

## Custom apps & bench groups

Teams can run their **own Frappe apps from private repositories** in customer-managed Release
Groups ("bench groups", like Frappe Cloud's Bench Groups): pick a Frappe major, marketplace apps
and custom apps, build an image, and run it as a Bench on one of the team's servers.

### DocTypes

| DocType | Naming | Notes |
|---|---|---|
| GitHub Settings | single | `github_app_id`, `app_slug`, `client_id`, `client_secret` (Password), `private_key` (Password, PEM), `webhook_secret` (Password) |
| GitHub Installation | `installation_id` | `team`, `account_login`, `account_type` User/Organization, `status` Active/Suspended/Deleted, `installed_by`, `repository_selection` |
| Custom App | hash | `team`, `app_name` (python package), `title`, `provider` GitHub/Other, `repo_url` (never with credentials), `repo_full_name`, `installation`, `branch`, `frappe_majors` ("14,15,16", empty = unchecked), `frappe_dependency`, `last_commit(_at/_message)`, `status` Active/Disabled; `access_username` + `access_token` (Password) for non-GitHub hosts |
| Release Group App | child | + `source_type` Marketplace/Custom, `custom_app`, `app_name` (python name of either kind) |
| Release Group | | + `update_available` (set by the push webhook, cleared by a successful build containing the latest commits) |
| Deploy Candidate | | + hidden `app_sources` (non-secret: custom app, installation, repo), `commits` |

Validation (`nexaploy.utils.check_group_apps`, pure): marketplace rows are unchanged (approved +
release for the major); a custom app must belong to the **same team** as the group (so shared,
teamless groups can never use one), be Active and, when it declares `frappe_majors`, support the
group's major; app names are unique across both kinds.

### GitHub App setup (admin)

1. GitHub → Settings → Developer settings → **New GitHub App**. Repository permissions:
   *Contents: Read-only*, *Metadata: Read-only*. Subscribe to **Push** (installation events are
   always sent).
2. *Callback URL* and *Setup URL*: `https://<cp>/api/method/nexaploy.api.github.installation_callback`;
   enable **Request user authorization (OAuth) during installation**.
3. *Webhook URL*: `https://<cp>/api/method/nexaploy.api.github.webhook`, with a webhook secret.
4. Fill **GitHub Settings** (App ID, slug, client ID + secret, private key PEM, webhook secret).

Dependency: `PyJWT[crypto]` (i.e. `cryptography`), already installed with Frappe; without it
`nexaploy.github_utils.build_app_jwt` raises a clear error.

### Flow

1. *Connect GitHub* → `nexaploy.api.github.install_url` → GitHub install page with
   `state` = HMAC-signed `{team, user, expiry 1 h}` (key derived from the site encryption key).
2. GitHub redirects to `installation_callback(installation_id, setup_action, state, code)`. It
   requires a login, checks the state (same user, write access to the team), and **proves the user
   can access the installation** by exchanging the OAuth `code` and listing `/user/installations`
   (without `client_secret` only platform admins can link installations — installation ids are
   guessable). An installation belongs to one team. Stored as **GitHub Installation**.
3. `list_repos(installation)` / `list_branches(installation, repo)` → `add_custom_app(installation,
   repo, branch)` reads `pyproject.toml` / `setup.py` / `<dir>/hooks.py` through the contents API to
   detect the app name, and `[tool.bench.frappe-dependencies] frappe` (else a `version-NN` branch
   name) for `frappe_majors`. Other hosts: `add_custom_app_from_url(repo_url, branch, app_name,
   access_token, access_username)`.
4. `nexaploy.api.benches.create_release_group(title, frappe_major, apps=[{source_type, app,
   branch}], build_server)`, `add_app`, `remove_app`, `deploy` (Deploy Candidate → `image.build`),
   `list_deploys`, `get_deploy` (build log / live steps), `deploy_to_server(release_group, server)`
   (creates a Bench on a team server, or `bench.update`s the group's existing bench there),
   `options(frappe_major)`, `update_release_group`. All team-scoped via `nexaploy.teams`
   (`read` / `write`); build and target servers must be the team's own (or the platform default
   build server).
5. **Push webhook** (`X-Hub-Signature-256` verified with the webhook secret, 401 otherwise): a
   push to a tracked branch updates `Custom App.last_commit` and sets `update_available` on every
   group whose row tracks that repo + branch; groups with `auto_update` start a build right away
   (and roll their benches inside `update_window` when it succeeds). `installation`
   deleted/suspend/unsuspend events update the installation status.

### Secrets never stored

`Deploy Candidate.apps_json` holds plain `https://github.com/owner/repo` URLs (also what
`bench.create` receives). The `image.build` job of a group with custom apps stores the placeholder
`__secret__:github:<deploy candidate>:apps_json`; `poll` resolves it through
`nexaploy.github_secrets.resolve`, which mints a **fresh, repository-scoped, read-only installation
token** (`POST /app/installations/{id}/access_tokens`, 1 h) per installation and returns
`https://x-access-token:<token>@github.com/owner/repo` URLs (other providers: the Custom App's
`access_token`). If GitHub is unreachable the credential-free list is sent and the clone fails
visibly. `image-builder/build.sh` redacts URL credentials and `ghp_/ghs_/github_pat_` tokens from
its output and log and writes a redacted `apps.json` copy; `.git` dirs are stripped from the image.
A build that waits more than ~1 h in the agent queue after `poll` fails on clone; retry it.

### Portal

`/dashboard/benches` (list, create, connect GitHub, custom apps) and `/dashboard/bench?name=`
(apps, deploys with live build log, *Update available* badge, deploy, benches on servers). JS:
`public/js/benches.js` (components `nxBenches`, `nxBench`). Arabic strings:
`translations/ar.github.part`.

## Activity log, prepaid credits, 2FA and API keys (Phase 4 security)

### Activity log (ARCHITECTURE.md §10)

`nexaploy.activity.log_activity(team, action, reference_doctype=None, reference_name=None,
details=None)` appends a **Team Activity** row (`team`, `user` = session user, `action`,
`reference_doctype`/`reference_name`, `details` JSON, `ip_address` = `frappe.local.request_ip`,
`creation`). It **never raises** (failures go to the Error Log) and runs in the caller's
transaction, so a rolled-back request leaves no row. `details` goes through
`team_utils.scrub_details`: keys containing password/secret/token/credential/authorization/
cookie/hmac/signature/api_key/access_key/private_key/session (and `sid`, `otp`, `pwd`, `auth`…)
are dropped, values that look like credentials (`token k:s`, `Bearer …`, `__secret__:` refs,
`sk_live_…`, PEM keys) are dropped, strings are cut at 2000 chars, collections at 100 items.
The bare key `key` is kept (site config keys). Actions are dotted lowercase verbs.

Logged today: `team.create|update|invite|invite_revoke|join|change_role|remove_member|leave|two_factor`,
`site.create`, `site.<action>` (backup, install_app, uninstall_app, add_domain, remove_domain,
migrate, drop, restore — from `api.dashboard.site_action`), `server.registration_token`,
`invoice.pay` (checkout started), `invoice.paid` (gateway webhook), `credits.topup` (checkout
started, and again with `status: paid` when credited), `credits.apply`, `credits.promo`,
`api_key.create|rotate|revoke` (logged to every team of the user).

Team Activity is read-only for customers and cannot be edited or deleted (controller guards;
System Manager has read/report/export only). Customers read it with the `admin` action
(Owner/Admin): `/dashboard/activity` (filter by action or `prefix.`, member, date range; 50 per
page) via `nexaploy.api.teams.get_activity`.

### Prepaid credits

**Team Credit Transaction** is an append-only ledger: `team`, `type`
(TopUp/InvoicePayment/Refund/Promo/Adjustment), signed `amount` (+ adds, − spends), `currency`,
`reference_doctype`/`reference_name`, `balance_after`, `description`. Rows cannot be changed or
deleted — correct mistakes with an `Adjustment` (System Managers may insert one in the desk;
the controller computes `balance_after` under the team lock). Balances are per (team,
currency) and never go below zero; `Team.credit_balance` caches the balance in the team
currency (`Team.currency`, else Billing Settings currency) and is recomputed on every ledger
insert and Team save. Pure math: `nexaploy/billing/credits_utils.py`; database side:
`nexaploy/billing/credits.py` (`get_balance`, `add_credits(team, amount, type, ref)`,
`apply_credits_to_invoice(invoice)`, `apply_credits_to_open_invoices`, `grant_signup_credits`,
`create_topup_invoice`, `credit_topup`).

- **Invoices** gain `invoice_type` (Subscription / Credit Top-up), `credits_applied` and
  `amount_due` (= total − credits). A new monthly invoice is paid from credits first: fully →
  Paid (a `Credits` Nexaploy Payment is recorded), partially → only `amount_due` is charged by
  the gateway (`pay_invoice` re-applies credits before starting a checkout; the underpayment
  check compares with `amount_due`).
- **Top-up**: `nexaploy.api.billing.topup(amount, gateway)` (billing action; Billing Settings
  `min_topup`/`max_topup`, default 5–10000) creates a *Credit Top-up* invoice in the team currency
  (no tax) and returns the hosted checkout URL. The normal Stripe/Paymob webhook marks it Paid;
  `mark_paid` then adds a `TopUp` ledger row (idempotent per invoice) and spends the new credits
  on open invoices, oldest due first. Top-up invoices are never marked Overdue nor suspend a
  team; unpaid ones are voided after 3 days by the daily dunning job (a late success still
  credits them).
- **Signup credits**: Billing Settings `signup_credits` (0 = off) are granted as a `Promo` row
  when a team is created (`teams.create_team`, so also personal teams) — once per team and,
  to stop farming by creating teams, once per owner user.
- **Portal**: `/dashboard/billing` shows the balance, a top-up form (Owner/Admin/Billing) and the
  ledger (`nexaploy.api.billing.get_credits`), plus credits applied / amount due per invoice.

### Two-factor authentication

Owners/Admins toggle **Require two-factor authentication** on `/dashboard/team`
(`nexaploy.api.teams.set_two_factor(required)`). `Team.on_update` (and `after_delete`) runs
`teams.sync_two_factor_roles` for current and former members: members of at least one enabled
team with `two_factor_required` get the Role **Nexaploy 2FA** (`two_factor_auth = 1`,
no desk access, created on first use); others lose it. This also covers invitations accepted
later, members removed/leaving, role changes and teams being disabled. When the role is first
given, `System Settings.enable_two_factor_auth` is switched on (method *OTP App* if none is
set) — this is site-wide but Frappe only prompts users whose roles have `two_factor_auth`.
`/dashboard/account` shows the user's own status (enforced, method, which teams require it).

Limitations:

- Enforcement is Frappe's login 2FA: the second factor is asked at the **next** login; existing
  sessions are not terminated.
- **API keys bypass 2FA** (Frappe token auth has no second factor). Teams that require 2FA
  should review who holds keys in the activity log (`api_key.*`).
- Frappe has no per-user "2FA enrolled" flag for OTP apps until the first OTP login; we report
  whether 2FA is *enforced*, not whether the user has already set up the authenticator.
- Turning on `enable_two_factor_auth` is done with `frappe.db.set_single_value` (no System
  Manager check) the first time a team requires 2FA. Platforms that do not want this can
  pre-configure System Settings; turning it off again re-disables enforcement everywhere.
- `Administrator` is always exempt (Frappe never asks it for a second factor).

### API keys

`/dashboard/account` → *API access* generates, rotates (new key **and** secret) or revokes the
user's Frappe key pair (`User.api_key` / `User.api_secret`, `frappe.generate_hash`), via
`nexaploy.api.account.generate_api_key(rotate)` / `revoke_api_key`. The secret is returned once.
A key acts as the user with their role in each team; choose the team per request:

```bash
curl -s https://cp.example.com/api/method/nexaploy.api.dashboard.list_sites \
  -H "Authorization: token <api_key>:<api_secret>" \
  -H "X-Nexaploy-Team: <team>"
```

The account page also edits first/last name and the portal language (`User.language`, en/ar —
Arabic renders RTL) and links to `/update-password`.

## Site lifecycle

Staging sites, moves between servers, transfers between teams and renames
(`nexaploy/api/site_lifecycle.py`, pure logic in `nexaploy/lifecycle_utils.py`, tests in
`tests/test_lifecycle.py`; portal tab *Lifecycle* on `/dashboard/site` and `/dashboard/transfer?token=`,
components `nxSiteLifecycle` / `nxTransfer` in `public/js/site_tools.js`, Arabic strings in
`translations/ar.lifecycle.part`). Every mutation is written to the Team Activity log
(`nexaploy.activity.log_activity`, imported lazily).

| Method | Access | What |
|---|---|---|
| `get_lifecycle(site)` | read | staging info, move targets (team benches/servers + shared hosted, with major compatibility), pending transfers, transfer block reason |
| `create_staging(site)` | write | Site `staging-<site>` (`is_staging`, `staging_of`, no plan, backups Off) on the same bench; `after_insert` enqueues `site.clone` instead of `site.create`. Re-creating after a drop reuses the archived staging document |
| `refresh_staging(site)` | write | `site.clone` again (staging data replaced). Drop a staging site with the normal drop action |
| `move_site(site, target_bench, allow_upgrade=0)` | write | `Site.move_to_bench`; returns the plan and, for another server, `dns_hint` (every host must point to the new server) |
| `request_transfer(site, email_or_team)` | admin | **Site Transfer** (token sha256, 7 days), emails `/dashboard/transfer?token=` to the team's members or the address |
| `get_transfer(token)` / `accept_transfer(token, team=None)` / `reject_transfer(token)` | admin of the target team (+ addressee for email transfers) | accept moves `Site.team` (and its staging site, Site Backup and Site Usage rows) to the target team; billing follows through the Site `on_update` hook |
| `cancel_transfer(name)` | admin of the source team | |
| `rename_site(site, new_subdomain_or_name, keep_old_domain=1)` | admin | status **Renaming**, `site.rename`; on success the callback `frappe.rename_doc("Site", old, new)` and adds the old name as an Active Site Domain |

Rules: staging sites are not moved, transferred or renamed themselves (they follow their production
site on transfer); a site can only change teams while it is on a **shared hosted bench** (bench and
server without a team) — sites on a team's own (BYOS) server must be moved first. Job params:

| kind | params | created by |
|---|---|---|
| `site.clone` | `{bench, site, target_site, scrub: true}` | `create_staging`, `refresh_staging` |
| `site.rename` | `{bench, site, new_name, keep_old_domain}` | `rename_site` |

Callbacks: `site.clone` → staging Site Active (apps Installed) / Broken; `site.rename` → document renamed,
Active / (failure in the `check` step) Active / otherwise Broken.

**Billing (owned by billing):** staging sites must not be billed — `billing.subscriptions.sync_site` has to
treat `doc.is_staging` like an archived site (cancel plan + app subscriptions), otherwise the default site
plan applies to them.

Not yet verified on a bench: `bench --site <new> restore` creating a site that does not exist yet (relied
on by moves; the agent's `site.restore` with `bench` documents it), `frappe.rename_doc` of Site from an agent
callback, custom site_config keys are not carried over by a move (only the encryption key and domains).

## Site observability (Phase 4: analytics, logs, slow queries, SQL, console)

Portal page **`/dashboard/insights?site=<site>`** (`www/dashboard/insights.{html,py}`, component
`nxInsights` in `public/js/insights.js`, included in the page `head` like `site_tools.js`). Tabs:
*Analytics* (requests / p50 / p95 / jobs, per-hour inline-SVG bar chart, top paths by count and total
time, status codes, background jobs by method), *Logs* (web / workers / scheduler / frappe log /
Error Log, line count, refresh), *Slow queries* (grouped by normalised query), *SQL* (textarea,
read-only toggle shown to admins only, results table, client-side CSV download) and *Console*
(admins only, warning banner, optional commit). Link to it from the site page with
`/dashboard/insights?site=<site>` (optionally `&tab=logs|slow|sql|console`).

Whitelisted API `nexaploy.api.observability` — every call queues one agent job referencing the Site and
returns `{job, status}`; the page polls `get_result(job)` (`{kind, status, done, error, data}`) every 1.5 s:

| Method | Team action | Agent job / params |
|---|---|---|
| `get_logs(site, log=web, lines=200, since=None, refresh=0)` | read | `site.logs {site, log, lines, since?}` |
| `get_analytics(site, days=7, refresh=0)` | read | `site.analytics {site, date_from, date_to}` (1–31 UTC days) |
| `get_slow_queries(site, limit=50, min_seconds=0, refresh=0)` | read | `site.slow_queries {site, limit, min_seconds}` |
| `run_sql(site, query, read_only=1, max_rows=500, timeout=30)` | write (read-only) / admin (write mode) | `site.sql {site, query, read_only, max_rows, timeout}` |
| `run_console(site, code, commit=0, timeout=60)` | admin | `site.console {site, code, commit, timeout, allow_shared_bench}` |
| `get_result(job)` | same action as the job needed | — |
| `get_permissions(site)` | read | `{read, sql, sql_write, console}` for the page |

- Read-only kinds (logs / analytics / slow queries) reuse an identical job younger than 60 s unless
  `refresh=1`; at most 5 unfinished observability jobs per site.
- SQL is validated before queueing (`observability_utils.validate_query`, a mirror of the agent's validator:
  read-only allows only SELECT/SHOW/DESCRIBE/EXPLAIN/WITH; client commands, backslashes and `/*! */` are
  always refused). The agent re-validates and runs it as the **site's own DB user**.
- The console is refused when the site's bench hosts sites of another team (the bench container can
  read every `site_config.json` of the bench); the agent independently refuses shared benches unless the
  CP sends `allow_shared_bench`.
- Every call is recorded with `nexaploy.activity.log_activity(team, "site.logs" | "site.analytics" |
  "site.slow_queries" | "site.sql" | "site.console", "Site", site, details)`; SQL / code text is truncated to
  2000 characters (`observability_utils.activity_details`).
- Results live in `Agent Job.result` (JSON); the agent caps them (logs 2 MB, SQL ≤ 5000 rows / 2000 chars
  per cell, console output 1 MB). User-level errors (SQL error, exception in console code) come back as
  `data.error` of a *successful* job; infrastructure errors fail the job.

Pure helpers: `nexaploy/observability_utils.py` (tests: `tests/test_observability.py`). Translations:
`translations/ar.observability.part`.

**Note for `nexaploy.api.dashboard.get_job`/`list_jobs`:** they show `result`/`params` of any job of the
team to every member with `read`, including `site.sql` / `site.console` results (and the query / code).
Restrict them for those kinds (e.g. `observability_utils.required_action(kind, params.read_only)`) if
Billing members must not see query results.
