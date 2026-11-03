# Nexaploy image builder (component B)

Builds **one Docker image per Release Group** (Frappe major + exact app list) —
a *Deploy Candidate* in Press terms. Contract: [`ARCHITECTURE.md` §4](../ARCHITECTURE.md).

```
image-builder/
├── Containerfile          # multi-stage: base → builder → backend
├── build.sh               # build entrypoint (called by the agent)
├── versions.json          # python/node/frappe_branch/debian per Frappe major
├── examples/apps-v1{4,5,6}.json
└── tests/test_build_sh.sh # dry-run tests, no Docker needed
```

On a server these files live in `/opt/nexaploy/images/` next to the upstream
`frappe_docker/` checkout that is used as build context.

## Usage

```bash
./build.sh --major 15 \
  --apps-json examples/apps-v15.json \
  --tag nexaploy/bench:erp-v15-main-20261103T2130 \
  --log /opt/nexaploy/images/builds/erp-v15-main-20261103T2130/build.log
```

| Flag | Default | Notes |
|---|---|---|
| `--major N` | required | must be a key of `versions.json` (14, 15, 16) |
| `--apps-json FILE` | required | JSON array of `{"url", "branch"}`; `[]` = frappe only |
| `--tag REPO:TAG` | required | explicit tag required; `<release_group>-<build_id>` is parsed into labels |
| `--frappe-branch` | `versions.json` | e.g. `version-15-hotfix` or a tag like `v15.40.0` |
| `--frappe-path` | `https://github.com/frappe/frappe` | fork of frappe |
| `--python`, `--node`, `--debian` | `versions.json` | |
| `--context DIR` | `<script dir>/frappe_docker` | auto-cloned when missing (`$FRAPPE_DOCKER_REF`, default `main`; a 40-char SHA is supported) |
| `--platform` | host | `linux/arm64`; multiple platforms need `--push` |
| `--release-group`, `--build-id` | parsed from tag | label overrides |
| `--no-cache` | off | |
| `--push` | off | buildx pushes directly; plain `docker build` runs `docker push` afterwards |
| `--log FILE` | none | build output appended; `apps.json` copied next to it (mode 600) |
| `--dry-run` | off | validate everything, print the command(s), print tag, exit 0 |

Environment: `FRAPPE_DOCKER_REF`, `FRAPPE_DOCKER_URL`, `NEXAPLOY_BUILDER`
(`buildx`/`docker`/`auto`), `NEXAPLOY_VERSIONS_JSON`, `NEXAPLOY_CONTAINERFILE`,
`WKHTMLTOPDF_VERSION`, `BENCH_VERSION` (pin `frappe-bench`).

**Output contract:** exit 0 on success and the image tag is the last stdout line.
All progress and docker output goes to stderr (and `--log`). Exit codes: `1` build/push
failed, `2` invalid input, `3` missing tool (docker/git), `4` bad build context.

### Validation

Before anything runs, `build.sh` checks (using `jq`, or `python3` when jq is absent):

- `--major` exists in `versions.json`;
- `apps.json` is an array of objects with non-empty string `url` (`https://`, `http://`,
  `ssh://`, `git://` or `git@host:path`) and `branch` (`[A-Za-z0-9][A-Za-z0-9._/-]*`),
  and does not list `frappe` itself;
- tag matches `[registry[:port]/]name[/name]:tag` (lowercase repo, no digests);
- version / branch / platform strings have sane formats (no shell metacharacters).

## How it works

`build.sh` runs (simplified):

```
docker buildx build --load|--push -f image-builder/Containerfile --target backend \
  -t <tag> --build-arg PYTHON_VERSION=… NODE_VERSION=… DEBIAN_BASE=… FRAPPE_BRANCH=… \
  FRAPPE_PATH=… FRAPPE_MAJOR=… RELEASE_GROUP=… BUILD_ID=… APPS_JSON_BASE64=… \
  /opt/nexaploy/images/frappe_docker
```

The **build context is the upstream frappe_docker checkout** and our Containerfile is
passed with `-f`. That is how the image gets upstream's nginx files:

| Upstream path (context) | In image |
|---|---|
| `resources/core/nginx/nginx-template.conf` | `/templates/nginx/frappe.conf.template` |
| `resources/core/nginx/nginx-entrypoint.sh` | `/usr/local/bin/nginx-entrypoint.sh` |
| `resources/core/nginx/security_headers.conf` | `/etc/nginx/snippets/security_headers.conf` |

The paths are ARGs (`RESOURCES_DIR`, `NGINX_TEMPLATE_PATH`, `NGINX_ENTRYPOINT_PATH`,
`NGINX_SECURITY_HEADERS_PATH`). If they aren't found, the old flat layout
(`resources/nginx-template.conf`) is used instead.

Stages:

1. **base**: `python:${PYTHON_VERSION}-slim-${DEBIAN_BASE}`, user `frappe` 3113:3113,
   nvm + Node + yarn, nginx (non-root ready), mariadb-client, redis-tools, rclone,
   restic, jq, wait-for-it, git, curl, wkhtmltopdf 0.12.6.1-3 with patched Qt (amd64 and
   arm64; skipped with a warning on other arches), `frappe-bench`.
2. **builder**: build deps; decodes `APPS_JSON_BASE64` → `/opt/frappe/apps.json`;
   `bench init --apps_path … --frappe-branch … --frappe-path …` (this also builds the
   assets); removes every `.git` directory; moves `sites/assets` →
   `/home/frappe/frappe-bench/assets` and keeps a copy of `sites/apps.txt` in `.nexaploy/`.
3. **backend** (final): copies the bench, `USER frappe`, `WORKDIR
   /home/frappe/frappe-bench`, `VOLUME sites logs`, `CMD` = gunicorn (same flags as
   upstream), OCI labels.

### Runtime notes for the bench template

- Benches bind-mount `./sites`, which hides anything the image has in `sites/`. That's why
  the image's `ENTRYPOINT` (`/usr/local/bin/nexaploy-entrypoint.sh`) symlinks
  `sites/assets` → the baked `assets/` and refreshes `sites/apps.txt` from the image
  every time a container starts (idempotent, atomic `mv`). This works the same way as
  upstream's `main-entrypoint.sh`. **Compose files must not override `entrypoint:`.**
  Override `command:` as usual (`nginx-entrypoint.sh`, `bench worker --queue short`,
  `bench schedule`, `node /home/frappe/frappe-bench/apps/frappe/socketio.js`).
- An image update to a new app list therefore updates `apps.txt` automatically.
  The agent still has to run `bench --site … migrate` / `install-app`.
- The frontend runs `nginx-entrypoint.sh` as UID 3113. `/etc/nginx/conf.d`,
  `/var/lib/nginx` and `/var/log/nginx` are owned by `frappe`.

### Labels

`org.opencontainers.image.{title,description,vendor,source,licenses,created,version,revision}`,
`io.nexaploy.frappe-major`, `io.nexaploy.frappe-branch`, `io.nexaploy.release-group`,
`io.nexaploy.build-id`. The agent can recover a bench's Release Group from
`docker image inspect`.

## Release Groups → builds

| Control plane | build.sh |
|---|---|
| Release Group `frappe_major` | `--major` (picks python/node/debian from `versions.json`) |
| Release Group `frappe_branch` | `--frappe-branch` (if it differs from the default) |
| Release Group Apps (`app` → Marketplace App `repo_url`, `branch`) | `apps.json` entries, in install order |
| Deploy Candidate `build_id` | tag suffix: `nexaploy/bench:<release_group>-<build_id>` |
| Deploy Candidate `build_log` | `--log /opt/nexaploy/images/builds/<tag-ref>/build.log` |

Every new Deploy Candidate gets a new `build_id`. `CACHE_BUST=$BUILD_ID` forces
`bench init` to re-run so it picks up the latest commits on the branches. The base layers
(OS, node, wkhtmltopdf) stay cached. For reproducible builds, pin apps to tags or
commits in `branch`. bench accepts any ref that `git clone --branch` accepts.

## Private repositories

Use a token in the URL, which the caller provides (ARCHITECTURE §4):

```json
[{"url": "https://x-access-token:<TOKEN>@github.com/acme/acme_app", "branch": "main"}]
```

- In the final image, all `.git` directories (including remotes with credentials) are
  removed, and `/opt/frappe/apps.json` is deleted in the builder stage. The final
  `backend` stage never sees `APPS_JSON_BASE64`.
- **But** the build arg is still visible in the builder stage's metadata and in the
  BuildKit cache on the build host. Use short-lived, read-only tokens (GitHub App
  installation tokens / fine-grained PATs). Run `docker builder prune` when this is a
  concern.
- `--dry-run` and the logged command redact `APPS_JSON_BASE64` when any URL contains
  `user@`. The copy of `apps.json` next to `--log` is mode 600.
- SSH URLs (`git@github.com:…`) pass validation but need an SSH agent/key inside the
  build, which this Containerfile does not set up. Use HTTPS tokens.

## How the agent invokes it

`nexaploy_agent/runtime/image.py` (job `image.build`, `POST /v1/images/build`
`{major, apps_json, tag, frappe_branch?}`):

1. Write `apps_json` to `/opt/nexaploy/images/builds/<tag-ref>/apps.json` (mode 600).
2. Run the argument list (no shell):

   ```python
   ["/opt/nexaploy/images/build.sh",
    "--major", str(major),
    "--apps-json", f"{builds}/{ref}/apps.json",
    "--tag", tag,
    "--context", "/opt/nexaploy/images/frappe_docker",
    "--log", f"{builds}/{ref}/build.log",
    *(["--frappe-branch", frappe_branch] if frappe_branch else [])]
   ```
3. Stream stderr into the job step output; on exit 0, the last non-empty stdout line is the
   tag. Exit 2 means bad input (don't retry). Exit 1 means build failure (see the log).

## Versions

| Major | Python | Node | Debian | Source of truth |
|---|---|---|---|---|
| 14 | 3.10.13 | 16.20.2 | bookworm | frappe `version-14`: `requires-python >=3.10`, node `>=14` |
| 15 | 3.11.6 | 18.20.2 | bookworm | frappe `version-15`: `requires-python >=3.10,<3.15`, node `>=18` |
| 16 | 3.14.10 | 24.22.2 | trixie | frappe `version-16`: `requires-python >=3.14,<3.15`, node `>=24`; upstream frappe_docker builds v16 on python 3.14 / node 24 / trixie |

If you bump patch versions, check that `python:<ver>-slim-<debian>` exists on Docker Hub.

## Tests

```bash
bash image-builder/tests/test_build_sh.sh   # dry-run tests, no Docker required
```
