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

`team` is a Link to **User** (the customer account). Users with the *Nexaploy Customer* role
only see rows whose `team` is themselves (`nexaploy/permissions.py`).

## Code map

| Path | What |
|---|---|
| `nexaploy/api/agent.py` | `register`, `heartbeat`, `poll`, `job_update` (§5.6) + `validate_agent_auth` auth hook |
| `nexaploy/api/marketplace.py` | `list_apps(frappe_major=None)` (guest-readable) |
| `nexaploy/agent_job.py` | `create_agent_job(server, kind, params, reference_doctype, reference_name)` |
| `nexaploy/agent_callbacks.py` | per-kind callbacks run by `job_update` |
| `nexaploy/utils.py` | pure logic (no frappe import): name regexes, token hashing, status mapping, compatibility, apps.json, move plan |
| `nexaploy/tasks.py` | scheduler: mark servers Offline after 5 min without heartbeat (every minute), prune expired unused registration tokens (hourly) |
| `tests/` | plain pytest, runs without a bench: `python -m pytest control-plane/tests` |

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
| `image.build` | `{major, apps_json:[{url,branch}], tag, frappe_branch}` | Deploy Candidate insert (on Release Group `build_server`) |
| `bench.create` | `{name, image, apps_json}` | Bench insert |
| `bench.start/stop/restart/remove` | `{bench[, force]}` | Bench methods |
| `bench.update` | `{bench, image}` | `Bench.update_image` |
| `site.create` | `{bench, name, admin_password, apps:[app_name]}` | Site insert, move |
| `site.install_app` / `site.uninstall_app` | `{site, app}` | Site methods |
| `site.add_domain` / `site.remove_domain` | `{site, domain}` | Site methods |
| `site.backup` | `{site, with_files, upload: true}` | Site Backup insert |
| `site.restore` | `{site, remote_path}` | move |
| `site.migrate` | `{site}` | `Site.migrate` |
| `site.drop` | `{site, skip_backup}` | `Site.drop`, move |

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

### Major upgrade (`Site.move_to_bench(target_bench)`)

Never in place (§1). The plan is stored on the Site (`move_steps`, `move_step_idx`,
`move_backup`, `move_target_bench`; status **Moving**) and each successful step's callback
enqueues the next one:

- different server: `site.backup` → `site.create` (target) → `site.restore {remote_path}` (target, migrates) → `site.drop {skip_backup}` (source)
- same server: `site.backup` → `site.drop` (source) → `site.create` → `site.restore`
  (the agent resolves a site's bench by scanning `benches/*/sites/<s>`, so a name can only live in one bench per server)

A failed backup aborts the move (site stays Active where it was). A later failure marks the
site **Broken**; the move backup is linked in `move_backup` for manual recovery. Restoring on
another server requires the target agent's rclone remote to reach the same bucket/prefix.

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
- permission query conditions for the *Nexaploy Customer* role.
