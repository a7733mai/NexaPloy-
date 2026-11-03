#!/usr/bin/env bash
# Nexaploy server installer (ARCHITECTURE.md §5.7).
#
#   curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
#     | sudo bash -s -- --control-plane https://cp.example.com --token <registration-token> \
#                       --acme-email ops@example.com
#
# Idempotent: re-running upgrades the agent and keeps existing config/secrets.
# Uninstalling the agent never stops benches or sites (no lock-in).
set -euo pipefail

ROOT=/opt/nexaploy
DEFAULT_SOURCE="https://github.com/a7733mai/NexaPloy-.git"
FRAPPE_DOCKER_URL="https://github.com/frappe/frappe_docker.git"

TOKEN=""
CONTROL_PLANE=""
SERVER_ID=""
ACME_EMAIL=""
AGENT_SOURCE=""
BRANCH="main"
FRAPPE_DOCKER_REF="main"
SKIP_PREFLIGHT=0
FORCE_REGISTER=0

usage() {
    cat <<'EOF'
Usage: install.sh [options]

  --token TOKEN            with --control-plane: one-time registration token;
                           without: the local API bearer token (default: random)
  --control-plane URL      control plane base URL (empty = standalone)
  --server-id ID           server id (default: assigned by CP or generated)
  --acme-email EMAIL       Let's Encrypt account e-mail for the proxy
  --agent-source SRC       git URL or local path of the Nexaploy repo (or its agent/ dir)
  --branch BRANCH          git branch/tag for --agent-source (default: main)
  --frappe-docker-ref REF  frappe_docker git ref used as image build context (default: main)
  --force-register         register again even if a server token is already stored
  --skip-preflight         skip arch/RAM/disk/port checks
  -h, --help               show this help
EOF
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

need_val() { [[ $# -ge 2 && -n "${2:-}" && "${2:0:2}" != "--" ]] || die "$1 requires a value"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --token)             need_val "$@"; TOKEN="$2"; shift 2 ;;
        --control-plane)     need_val "$@"; CONTROL_PLANE="${2%/}"; shift 2 ;;
        --server-id)         need_val "$@"; SERVER_ID="$2"; shift 2 ;;
        --acme-email)        need_val "$@"; ACME_EMAIL="$2"; shift 2 ;;
        --agent-source)      need_val "$@"; AGENT_SOURCE="$2"; shift 2 ;;
        --branch)            need_val "$@"; BRANCH="$2"; shift 2 ;;
        --frappe-docker-ref) need_val "$@"; FRAPPE_DOCKER_REF="$2"; shift 2 ;;
        --force-register)    FORCE_REGISTER=1; shift ;;
        --skip-preflight)    SKIP_PREFLIGHT=1; shift ;;
        -h|--help)           usage; exit 0 ;;
        *)                   usage >&2; die "unknown option: $1" ;;
    esac
done

[[ -z "$SERVER_ID" || "$SERVER_ID" =~ ^[a-z0-9][a-z0-9-]{1,62}$ ]] || die "invalid --server-id: $SERVER_ID"
[[ -z "$ACME_EMAIL" || "$ACME_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || die "invalid --acme-email"
[[ -z "$CONTROL_PLANE" || "$CONTROL_PLANE" =~ ^https?:// ]] || die "--control-plane must be an http(s) URL"
if [[ -n "$CONTROL_PLANE" && -z "$TOKEN" && ! -f "$ROOT/config/agent.toml" ]]; then
    die "--control-plane needs --token <registration token>"
fi

AGENT_HOME="$ROOT/agent"
SRC_DIR="$AGENT_HOME/src"
VENV="$AGENT_HOME/.venv"
CONFIG="$ROOT/config/agent.toml"
export NEXAPLOY_CONFIG="$CONFIG"

# --------------------------------------------------------------------------- 1. preflight
preflight() {
    [[ $EUID -eq 0 ]] || die "run as root (sudo)"
    . /etc/os-release 2>/dev/null || die "cannot read /etc/os-release"
    case "${ID:-}:${VERSION_ID:-}" in
        ubuntu:22.04|ubuntu:24.04) ;;
        debian:12) warn "Debian 12 is supported on a best-effort basis" ;;
        *) [[ $SKIP_PREFLIGHT -eq 1 ]] || die "unsupported OS ${PRETTY_NAME:-unknown} (need Ubuntu 22.04/24.04 or Debian 12; --skip-preflight to override)" ;;
    esac
    command -v apt-get >/dev/null || die "apt-get not found"
    [[ $SKIP_PREFLIGHT -eq 1 ]] && { warn "skipping preflight checks"; return; }

    case "$(uname -m)" in
        x86_64|aarch64) ;;
        *) die "unsupported architecture $(uname -m) (need x86_64 or aarch64)" ;;
    esac
    local mem_kb disk_kb
    mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
    (( mem_kb >= 1900000 )) || die "need >= 2 GB RAM (found $((mem_kb / 1024)) MB)"
    mkdir -p "$ROOT"
    disk_kb=$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')
    if [[ ! -f "$CONFIG" ]]; then
        (( disk_kb >= 20 * 1024 * 1024 )) || die "need >= 20 GB free on $ROOT (found $((disk_kb / 1024 / 1024)) GB)"
    fi
    local port
    for port in 80 443; do
        if ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
            if command -v docker >/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx nexaploy-traefik; then
                continue  # our own proxy (re-run)
            fi
            die "port $port is already in use: $(ss -ltnpH "sport = :$port" | head -1)"
        fi
    done
}

# --------------------------------------------------------------------------- 2. packages
install_packages() {
    export DEBIAN_FRONTEND=noninteractive
    log "installing packages"
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg git jq acl rclone openssl iproute2 >/dev/null

    if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
        log "installing Docker CE (official repository)"
        . /etc/os-release
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update -qq
        apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
    fi
    systemctl enable --now docker >/dev/null

    PYTHON=""
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
            PYTHON="$(command -v "$candidate")"; break
        fi
    done
    if [[ -z "$PYTHON" ]]; then
        log "installing python3.11 (system python is older than 3.11)"
        apt-get install -y -qq python3.11 python3.11-venv >/dev/null || die "cannot install python3.11"
        PYTHON="$(command -v python3.11)"
    fi
    local ver; ver="$("$PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    apt-get install -y -qq "python${ver}-venv" >/dev/null 2>&1 || apt-get install -y -qq python3-venv >/dev/null
}

# --------------------------------------------------------------------------- 3. users + tree
ensure_user() {  # name uid
    local name=$1 uid=$2
    getent group "$uid" >/dev/null || groupadd --system --gid "$uid" "$name"
    getent passwd "$uid" >/dev/null || useradd --system --uid "$uid" --gid "$uid" \
        --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$name"
}

mkdir_owned() {  # path uid gid mode
    install -d -o "$2" -g "$3" -m "$4" "$1"
}

create_tree() {
    log "creating users and $ROOT"
    ensure_user nexaploy-traefik 3100
    ensure_user nexaploy-mariadb 3101
    ensure_user nexaploy-redis 3102
    ensure_user nexaploy-frappe 3113

    mkdir_owned "$ROOT" 0 0 0755
    mkdir_owned "$ROOT/config" 0 0 0700
    mkdir_owned "$AGENT_HOME" 0 0 0755
    mkdir_owned "$AGENT_HOME/data" 0 0 0700
    mkdir_owned "$ROOT/proxy" 0 0 0755
    mkdir_owned "$ROOT/proxy/config" 0 0 0755
    mkdir_owned "$ROOT/proxy/config/traefik" 0 0 0755
    mkdir_owned "$ROOT/proxy/config/sites" 0 0 0755
    mkdir_owned "$ROOT/proxy/data" 3100 3100 0750
    mkdir_owned "$ROOT/proxy/data/certificates" 3100 3100 0700
    mkdir_owned "$ROOT/proxy/logs" 3100 3100 0750
    [[ -f "$ROOT/proxy/data/certificates/acme.json" ]] || install -o 3100 -g 3100 -m 0600 /dev/null "$ROOT/proxy/data/certificates/acme.json"
    chown 3100:3100 "$ROOT/proxy/data/certificates/acme.json"; chmod 600 "$ROOT/proxy/data/certificates/acme.json"
    mkdir_owned "$ROOT/db" 0 0 0755
    mkdir_owned "$ROOT/db/data" 3101 3101 0700
    mkdir_owned "$ROOT/images" 0 0 0755
    mkdir_owned "$ROOT/images/builds" 0 0 0700
    mkdir_owned "$ROOT/benches" 0 0 0755
    mkdir_owned "$ROOT/backups" 3113 3113 0750
}

# --------------------------------------------------------------------------- 4. networks
create_networks() {
    local net
    for net in proxy-network nexaploy-db; do
        docker network inspect "$net" >/dev/null 2>&1 || { log "creating docker network $net"; docker network create --driver bridge "$net" >/dev/null; }
    done
}

# --------------------------------------------------------------------------- 5. agent
fetch_source() {
    local script_dir src
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
    src="$AGENT_SOURCE"
    if [[ -z "$src" && -n "$script_dir" && -f "$script_dir/../pyproject.toml" ]]; then
        src="$(cd "$script_dir/../.." && pwd)"   # running from a checkout: agent/install/install.sh
    fi
    src="${src:-$DEFAULT_SOURCE}"

    if [[ -d "$src" ]]; then
        src="$(cd "$src" && pwd)"
        [[ "$src" == "$SRC_DIR" ]] && return
        log "copying agent source from $src"
        rm -rf "$SRC_DIR.new"
        mkdir -p "$SRC_DIR.new"
        tar -C "$src" --exclude=.venv --exclude=.git --exclude='__pycache__' --exclude='*.egg-info' -cf - . \
            | tar -C "$SRC_DIR.new" -xf -
        rm -rf "$SRC_DIR"; mv "$SRC_DIR.new" "$SRC_DIR"
    else
        log "fetching agent source $src ($BRANCH)"
        if [[ -d "$SRC_DIR/.git" ]]; then
            git -C "$SRC_DIR" remote set-url origin "$src"
            git -C "$SRC_DIR" fetch --depth 1 origin "$BRANCH"
            git -C "$SRC_DIR" reset --hard FETCH_HEAD
        else
            rm -rf "$SRC_DIR"
            git clone --depth 1 --branch "$BRANCH" "$src" "$SRC_DIR"
        fi
    fi
    if [[ -f "$SRC_DIR/agent/pyproject.toml" ]]; then
        PKG_DIR="$SRC_DIR/agent"; REPO_DIR="$SRC_DIR"
    elif [[ -f "$SRC_DIR/pyproject.toml" ]]; then
        PKG_DIR="$SRC_DIR"; REPO_DIR="$(dirname "$SRC_DIR")"
    else
        die "no agent pyproject.toml found in $SRC_DIR"
    fi
}

install_agent() {
    fetch_source
    # Ship the canonical bench-template files with the package when available.
    if [[ -d "$REPO_DIR/bench-template" && -x "$PKG_DIR/scripts/sync_templates.sh" ]]; then
        "$PKG_DIR/scripts/sync_templates.sh" "$REPO_DIR/bench-template" >/dev/null
    fi
    log "installing agent into $VENV"
    [[ -x "$VENV/bin/python" ]] || "$PYTHON" -m venv "$VENV"
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q --upgrade "$PKG_DIR"
    ln -sf "$VENV/bin/nexaploy" /usr/local/bin/nexaploy
    ln -sf "$VENV/bin/nexaploy-agent" /usr/local/bin/nexaploy-agent
    TEMPLATES="$("$VENV/bin/python" -c 'from nexaploy_agent.render import templates_dir; print(templates_dir())')"
}

install_stacks() {
    local tpl="$TEMPLATES"
    [[ -f "$tpl/proxy/docker-compose.yml" ]] || tpl="$REPO_DIR/bench-template"
    [[ -f "$tpl/proxy/docker-compose.yml" && -f "$tpl/db/docker-compose.yml" ]] \
        || die "proxy/db stack templates not found (looked in $TEMPLATES and $REPO_DIR/bench-template)"
    log "installing proxy and db stacks from $tpl"

    install -m 0644 "$tpl/db/docker-compose.yml" "$ROOT/db/docker-compose.yml"
    [[ -f "$ROOT/db/.env" ]] || install -m 0644 "$tpl/db/env.example" "$ROOT/db/.env"

    install -m 0644 "$tpl/proxy/docker-compose.yml" "$ROOT/proxy/docker-compose.yml"
    if [[ -d "$tpl/proxy/config/traefik" ]]; then
        cp -a "$tpl/proxy/config/traefik/." "$ROOT/proxy/config/traefik/"
    fi
    [[ -f "$ROOT/proxy/.env" ]] || install -m 0644 "$tpl/proxy/env.example" "$ROOT/proxy/.env"
    if [[ -n "$ACME_EMAIL" ]]; then
        set_env_var "$ROOT/proxy/.env" ACME_EMAIL "$ACME_EMAIL"
    elif grep -q '^ACME_EMAIL=admin@example.com' "$ROOT/proxy/.env"; then
        warn "ACME_EMAIL is still admin@example.com; pass --acme-email"
    fi

    # Image builder (component B) -> /opt/nexaploy/images
    if [[ -f "$REPO_DIR/image-builder/build.sh" ]]; then
        local f
        for f in build.sh Containerfile versions.json; do
            [[ -f "$REPO_DIR/image-builder/$f" ]] && install -m 0644 "$REPO_DIR/image-builder/$f" "$ROOT/images/$f"
        done
        chmod 0755 "$ROOT/images/build.sh"
        if [[ -d "$REPO_DIR/image-builder/resources" ]]; then
            cp -a "$REPO_DIR/image-builder/resources" "$ROOT/images/"
        fi
    else
        warn "image-builder/build.sh not found in the agent source; image.build jobs will fail"
    fi
    if [[ ! -d "$ROOT/images/frappe_docker/.git" ]]; then
        log "cloning frappe_docker ($FRAPPE_DOCKER_REF)"
        git clone --quiet "$FRAPPE_DOCKER_URL" "$ROOT/images/frappe_docker"
        git -C "$ROOT/images/frappe_docker" checkout --quiet "$FRAPPE_DOCKER_REF"
    fi
}

set_env_var() {  # file key value
    local file=$1 key=$2 value=$3 tmp
    tmp="$(mktemp)"
    awk -v k="$key" -v v="$value" 'BEGIN{done=0} $0 ~ "^"k"=" {print k"="v; done=1; next} {print} END{if(!done) print k"="v}' "$file" > "$tmp"
    cat "$tmp" > "$file"; rm -f "$tmp"
}

# --------------------------------------------------------------------------- 6. config
write_config() {
    log "writing $CONFIG"
    [[ -f "$ROOT/config/db.env" ]] || {
        umask 077
        printf 'DB_ROOT_PASSWORD=%s\n' "$(openssl rand -hex 24)" > "$ROOT/config/db.env"
        umask 022
    }
    chmod 600 "$ROOT/config/db.env"
    [[ -f "$ROOT/config/rclone.conf" ]] || install -m 0600 /dev/null "$ROOT/config/rclone.conf"
    chmod 600 "$ROOT/config/rclone.conf"

    # Merge flags into agent.toml, keeping existing values (and secrets).
    NX_TOKEN="$TOKEN" NX_CP="$CONTROL_PLANE" NX_SERVER_ID="$SERVER_ID" NX_ROOT="$ROOT" \
    "$VENV/bin/python" - "$CONFIG" <<'PY'
import os, secrets, sys, tomllib
from pathlib import Path
from nexaploy_agent.config import update_config_file

path = Path(sys.argv[1])
cur = tomllib.loads(path.read_text()) if path.exists() else {}
agent = cur.get("agent", {})
cp = cur.get("control_plane", {})
token, cp_url, server_id = os.environ["NX_TOKEN"], os.environ["NX_CP"], os.environ["NX_SERVER_ID"]

upd_agent = {
    "server_id": server_id or agent.get("server_id") or f"srv-{secrets.token_hex(4)}",
    # With a control plane --token is a registration token, not the local API token.
    "token": (token if token and not cp_url else None) or agent.get("token") or secrets.token_urlsafe(32),
    "bind": agent.get("bind", "127.0.0.1"),
    "port": agent.get("port", 8787),
    "root": agent.get("root", os.environ["NX_ROOT"]),
}
upd_cp = {
    "url": cp_url or cp.get("url", ""),
    "token": cp.get("token", ""),
    "heartbeat_seconds": cp.get("heartbeat_seconds", 60),
    "poll_seconds": cp.get("poll_seconds", 10),
}
backup = cur.get("backup", {})
upd_backup = {
    "remote": backup.get("remote", "nexaploy-backups"),
    "prefix": backup.get("prefix") or upd_agent["server_id"],
    "retention_days": backup.get("retention_days", 14),
}
update_config_file(path, {"agent": upd_agent, "control_plane": upd_cp, "backup": upd_backup})
PY
    chmod 600 "$CONFIG"
}

register() {
    [[ -n "$CONTROL_PLANE" && -n "$TOKEN" ]] || return 0
    local existing
    existing="$("$VENV/bin/python" -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb")).get("control_plane",{}).get("token",""))' "$CONFIG")"
    if [[ -n "$existing" && $FORCE_REGISTER -eq 0 ]]; then
        log "already registered with the control plane (use --force-register to redo)"
        return 0
    fi
    log "registering with $CONTROL_PLANE"
    "$VENV/bin/nexaploy-agent" register --control-plane "$CONTROL_PLANE" --token "$TOKEN" --config "$CONFIG"
}

# --------------------------------------------------------------------------- 7. services
install_units() {
    log "installing systemd units"
    local unit unit_dir
    unit_dir="$PKG_DIR/install"
    for unit in nexaploy-agent-api.service nexaploy-agent-worker.service; do
        install -m 0644 "$unit_dir/$unit" "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
    for unit in nexaploy-agent-api nexaploy-agent-worker; do
        systemctl enable "$unit" >/dev/null 2>&1
        systemctl restart "$unit"
    done
}

start_stacks() {
    log "starting shared MariaDB (nexaploy-db)"
    docker compose -p nexaploy-db --project-directory "$ROOT/db" up -d --remove-orphans
    log "starting proxy (nexaploy-proxy)"
    docker compose -p nexaploy-proxy --project-directory "$ROOT/proxy" up -d --remove-orphans
    local i status=""
    for i in $(seq 1 60); do
        status="$(docker inspect -f '{{.State.Health.Status}}' nexaploy-db 2>/dev/null || true)"
        [[ "$status" == healthy ]] && break
        sleep 2
    done
    [[ "$status" == healthy ]] || warn "nexaploy-db is not healthy yet (status: ${status:-unknown})"
}

summary() {
    local i
    for i in $(seq 1 15); do
        nexaploy ping >/dev/null 2>&1 && break
        sleep 1
    done
    echo
    log "Nexaploy installed"
    echo "  root:          $ROOT"
    echo "  config:        $CONFIG"
    echo "  control plane: ${CONTROL_PLANE:-standalone}"
    echo "  API:           $(nexaploy-agent version 2>/dev/null) on 127.0.0.1:8787"
    echo
    nexaploy ping || warn "agent API not answering yet; check: journalctl -u nexaploy-agent-api"
}

main() {
    preflight
    install_packages
    create_tree
    create_networks
    install_agent
    install_stacks
    write_config
    register
    install_units
    start_stacks
    summary
}

main "$@"
