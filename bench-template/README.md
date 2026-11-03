# Nexaploy bench template (component A)

Docker Compose building blocks for one Nexaploy server, per
[`ARCHITECTURE.md`](../ARCHITECTURE.md) §2–§3:

| Path | Installed to | Purpose |
|---|---|---|
| `bench/docker-compose.yml.j2` | `/opt/nexaploy/benches/<bench>/docker-compose.yml` | One bench (compose project `<bench>`), rendered by the agent |
| `bench/env.example` | `/opt/nexaploy/benches/<bench>/.env` | Bench variables (§3.1) |
| `db/docker-compose.yml`, `db/env.example` | `/opt/nexaploy/db/` | Shared MariaDB 10.6 (`nexaploy-db`) |
| `proxy/docker-compose.yml`, `proxy/env.example` | `/opt/nexaploy/proxy/` | Traefik v3 + read-only docker socket proxy (+ optional CrowdSec) |
| `proxy/config/traefik/dynamic.yml` | `/opt/nexaploy/proxy/config/traefik/` | Middlewares `security-headers`, `rate-limit`, `compress`; TLS defaults |
| `site-route.yml.j2` | `/opt/nexaploy/proxy/config/sites/<site>.yml` | Traefik file-provider route for one site |
| `tests/test_templates.py` | — | Render + structure tests |

## Bench stack

Services (container `<bench>-<service>`): `backend` (gunicorn :8000),
`frontend` (nginx :8080), `websocket` (socket.io :9000), `queue-short`,
`queue-long`, `queue-default`, `scheduler`, `redis-cache`, `redis-queue`, and
the one-shot `configurator` (profile `configurator`, run with
`docker compose run --rm configurator`, no fixed container name).

Networks: `<bench>-net` (internal), `proxy-network` (external, frontend only),
`nexaploy-db` (external, backend/workers/scheduler/configurator). Egress for
backend/workers (mail, integrations) goes via `nexaploy-db`, which is a normal
bridge; `<bench>-net` itself has no egress.

Templating: the only Jinja variable is **`bench`** (it names containers,
networks and the redis hosts). All values — image, UIDs, TZ, DB host/port,
worker counts, CPU/memory limits — are `${VAR}` interpolations resolved by
Compose from the bench `.env`, so most changes are `.env` edit + `up -d`.
Extra context keys passed by the agent are ignored.

Hardening: fixed `user:` (frappe 3113, redis 3102), `read_only` root fs + `tmpfs`
everywhere except `frontend` (its entrypoint renders the nginx config at
start), `no-new-privileges`, `cap_drop: [ALL]`, `deploy.resources.limits`,
`logging: local` with rotation, healthchecks, `restart: unless-stopped`, no
published ports, no Traefik labels.

Bind mounts (relative to the bench dir, created by the agent):

| Host | Container | Owner |
|---|---|---|
| `./sites` | `/home/frappe/frappe-bench/sites` | 3113 |
| `./logs` | `/home/frappe/frappe-bench/logs` | 3113 |
| `./redis-queue` | `/data` (redis-queue AOF) | 3102 |

Plus a named volume `assets` (`<bench>_assets`) on `sites/assets`: the image
ships built assets inside the sites dir, which the `./sites` bind mount hides,
so Docker seeds this volume from the image on first use. **After changing the
image (bench update) remove it so it is re-seeded:**
`docker compose down && docker volume rm <bench>_assets && docker compose up -d`.

The configurator writes `sites/apps.txt` (from the image's `apps/`) and sets
`db_host`, `db_port`, `redis_cache`, `redis_queue`, `redis_socketio`,
`socketio_port` in `common_site_config.json` with `bench set-config -g`.
It is idempotent; re-run it after an image update or a DB host change.

## Manual Phase-0 bring-up

Normally `install.sh` and the agent do all of this. By hand, as root:

```bash
# 0. users + tree (UIDs from §2)
for u in traefik:3100 mariadb:3101 redis:3102 frappe:3113; do
  groupadd -g ${u#*:} nx-${u%:*} 2>/dev/null; useradd -r -u ${u#*:} -g ${u#*:} -s /usr/sbin/nologin nx-${u%:*} 2>/dev/null
done
mkdir -p /opt/nexaploy/{config,proxy,db,benches,backups}
cp -r bench-template/proxy/. /opt/nexaploy/proxy/
cp bench-template/db/docker-compose.yml /opt/nexaploy/db/

# 1. networks
docker network create proxy-network
docker network create nexaploy-db

# 2. shared DB
umask 077; echo "DB_ROOT_PASSWORD=$(openssl rand -hex 24)" > /opt/nexaploy/config/db.env
cp bench-template/db/env.example /opt/nexaploy/db/.env
mkdir -p /opt/nexaploy/db/data && chown 3101:3101 /opt/nexaploy/db/data
docker compose --project-directory /opt/nexaploy/db up -d

# 3. proxy
cd /opt/nexaploy/proxy
cp env.example .env && sed -i 's/admin@example.com/you@your.domain/' .env
mkdir -p config/sites data/certificates logs
touch data/certificates/acme.json && chmod 600 data/certificates/acme.json
chown -R 3100:3100 config data logs
docker compose up -d                # add COMPOSE_PROFILES=crowdsec for CrowdSec

# 4. bench
B=erp-v15-main; D=/opt/nexaploy/benches/$B
mkdir -p $D/sites $D/logs $D/redis-queue
chown 3113:3113 $D/sites $D/logs && chown 3102:3102 $D/redis-queue
cp bench-template/bench/env.example $D/.env      # edit BENCH_NAME, IMAGE, ...
python3 -c "import jinja2,sys; e=jinja2.Environment(undefined=jinja2.StrictUndefined); \
print(e.from_string(open(sys.argv[1]).read()).render(bench=sys.argv[2]))" \
  bench-template/bench/docker-compose.yml.j2 $B > $D/docker-compose.yml
cd $D
docker compose run --rm configurator
docker compose up -d

# 5. site
. /opt/nexaploy/config/db.env
docker compose exec -T backend bench new-site erp.example.com \
  --mariadb-user-host-login-scope='%' --db-root-password "$DB_ROOT_PASSWORD" \
  --admin-password 'change-me' --install-app erpnext

# 6. route (Traefik picks it up immediately; cert issued on first request)
python3 -c "import jinja2,sys; e=jinja2.Environment(undefined=jinja2.StrictUndefined); \
print(e.from_string(open(sys.argv[1]).read()).render(site=sys.argv[2], domains=[sys.argv[2]], bench=sys.argv[3]))" \
  bench-template/site-route.yml.j2 erp.example.com $B > /opt/nexaploy/proxy/config/sites/erp.example.com.yml
```

(Use `--mariadb-user-host-login-scope='%'` on Frappe v15+; on v14 use
`--no-mariadb-socket`.) DNS for the domain must point to the server before
step 6 so the HTTP-01 challenge succeeds.

## Proxy notes

- Static config is entirely command flags; Traefik listens on container ports
  8080/8443 (published as `HTTP_PORT`/`HTTPS_PORT`, default 80/443) so it needs
  no capabilities as UID 3100. Ping is on :8082 (healthcheck), Prometheus
  metrics on :9100 — neither is published; scrape over `proxy-network`.
- File provider watches `/etc/traefik/dynamic` (`config/traefik` and
  `config/sites` mounted beneath it). Adding/removing `config/sites/<site>.yml`
  is all the agent does to route a site.
- `:80` redirects to `websecure` globally; the ACME HTTP challenge still works.
- The socket proxy exposes only read endpoints (`POST=0`) on an internal
  network; the Docker provider has `exposedByDefault=false`.
- CrowdSec (`COMPOSE_PROFILES=crowdsec`) only analyses `logs/access.log`;
  wire a bouncer separately to enforce decisions.

## Tests

```bash
pip install jinja2 pyyaml pytest
python -m pytest bench-template/tests
```

Renders both templates with `StrictUndefined`, parses every compose file and
asserts services, container names, networks, mounts, UIDs, hardening, absence
of Traefik labels and route structure. If `docker compose` is available the
files are also validated with `docker compose config` (otherwise skipped).
