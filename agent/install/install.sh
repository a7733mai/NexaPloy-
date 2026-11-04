#!/usr/bin/env bash
# Nexaploy server installer (ARCHITECTURE.md §5.7).
#
#   curl -fsSL https://raw.githubusercontent.com/a7733mai/NexaPloy-/main/agent/install/install.sh \
#     | sudo bash -s -- --control-plane https://cp.example.com --token <registration-token> \
#                       --acme-email ops@example.com
#
# All-in-one (the control plane runs on this same server and manages it):
#
#   curl -fsSL .../install.sh | sudo bash -s -- --with-control-plane cp.example.com \
#                                                --acme-email ops@example.com
#
# Without any option and with a terminal, it asks: new control plane here, or join one?
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
SKIP_PACKAGES=0
NO_SYSTEMD=0
SKIP_FRAPPE_DOCKER=0
OS_UPGRADE=1
HWE_KERNEL=0
REBOOT=auto        # auto: reboot at the end when the OS upgrade needs it; no: never
CP_DOMAIN=""
# Empty = automatic: <repo>:<agent version>, then :16, then :latest, else built locally from the
# official frappe/erpnext image (resolve_cp_image). --control-plane-image IMG forces one image.
CP_IMAGE="${NEXAPLOY_CONTROL_PLANE_IMAGE:-}"
CP_IMAGE_REPO="${NEXAPLOY_CONTROL_PLANE_REPO:-ghcr.io/a7733mai/nexaploy-control-plane}"
CP_FRAPPE_MAJOR=16
CP_OFFICIAL_FALLBACK="frappe/erpnext:v16.53.0"   # when image-builder/versions.json has no official_image
CP_BENCH="nexaploy-cp"
CP_PORT=8090
CP_ADMIN_PASSWORD=""
CP_RESTORE=""
SKIP_DNS_CHECK=0
ASSUME_YES=0
FIREWALL=1
FAIL2BAN=1

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
  --root DIR               install root (default: /opt/nexaploy; other paths are for testing)
  --skip-packages          do not install OS packages (Docker, rclone, ... must already exist)
  --no-systemd             do not install systemd units (run `nexaploy-agent api|worker` yourself)
  --skip-frappe-docker     do not clone frappe_docker (no image builds on this server)

 Server hardening:
  --no-firewall            do not enable ufw (default: deny incoming except SSH, 80, 443)
  --no-fail2ban            do not install fail2ban (default: SSH brute-force protection)

 Server update (done first, before anything is installed):
  --no-os-upgrade          do not run the full system upgrade (apt full-upgrade incl. kernel)
  --hwe-kernel             Ubuntu: also install the newer HWE kernel (linux-generic-hwe-<ver>)
  --no-reboot              never reboot; by default the server reboots 1 minute after a
                           successful install when the upgrade requires it (new kernel)

 All-in-one (control plane on this server):
  --with-control-plane DOMAIN  also install the control plane at https://DOMAIN (a bench +
                           site managed by this server's agent) and register this server to it
  --control-plane-image IMG  control plane image (default: automatic - the published image
                           ghcr.io/a7733mai/nexaploy-control-plane:<agent version>, :16 or :latest,
                           else built on this server from the official frappe/erpnext image)
  --control-plane-port PORT  127.0.0.1 port the agent uses to reach it (default: 8090)
  --admin-password PASS    Administrator password of the control plane (default: random,
                           saved in <root>/config/control-plane.env)
  --restore-control-plane SRC  create the control plane site from a backup instead of a new one:
                           a tarball from 'nexaploy controlplane backup', a backup directory,
                           an https:// URL of a tarball or an rclone remote:path (moving the
                           control plane to a new server; the other servers keep their tokens)
  --skip-dns-check         do not check that DOMAIN points to this server
  -y, --yes                never ask questions (no interactive setup)
  -h, --help               show this help
EOF
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

need_val() { [[ $# -ge 2 && -n "${2:-}" && "${2:0:2}" != "--" ]] || die "$1 requires a value"; }

ORIG_ARGC=$#
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
        --root)              need_val "$@"; ROOT="${2%/}"; shift 2 ;;
        --skip-packages)     SKIP_PACKAGES=1; shift ;;
        --no-systemd)        NO_SYSTEMD=1; shift ;;
        --skip-frappe-docker) SKIP_FRAPPE_DOCKER=1; shift ;;
        --no-os-upgrade)     OS_UPGRADE=0; shift ;;
        --hwe-kernel)        HWE_KERNEL=1; shift ;;
        --no-reboot)         REBOOT=no; shift ;;
        --no-firewall)       FIREWALL=0; shift ;;
        --no-fail2ban)       FAIL2BAN=0; shift ;;
        --with-control-plane) need_val "$@"; CP_DOMAIN="${2,,}"; shift 2 ;;
        --control-plane-image) need_val "$@"; CP_IMAGE="$2"; shift 2 ;;
        --control-plane-port) need_val "$@"; CP_PORT="$2"; shift 2 ;;
        --admin-password)    need_val "$@"; CP_ADMIN_PASSWORD="$2"; shift 2 ;;
        --restore-control-plane) need_val "$@"; CP_RESTORE="$2"; shift 2 ;;
        --skip-dns-check)    SKIP_DNS_CHECK=1; shift ;;
        -y|--yes)            ASSUME_YES=1; shift ;;
        -h|--help)           usage; exit 0 ;;
        *)                   usage >&2; die "unknown option: $1" ;;
    esac
done

# ---- interactive setup: only when started without options and a terminal is available
# (also under `curl ... | sudo bash`, reading answers from /dev/tty)
if [[ $ORIG_ARGC -eq 0 && $ASSUME_YES -eq 0 ]] && ( : </dev/tty ) 2>/dev/null; then
    exec 3</dev/tty
    ask() {  # prompt default -> REPLY
        local answer
        printf '%s' "$1" >/dev/tty
        IFS= read -r answer <&3 || answer=""
        REPLY="${answer:-$2}"
    }
    printf '\nNexaploy setup\n  1) New: install the control plane on this server (all-in-one)\n  2) Join: connect this server to an existing control plane\n  3) Standalone agent only (CLI, no control plane)\n' >/dev/tty
    ask "Choose [1]: " 1
    case "$REPLY" in
        1) ask "Control plane domain (DNS A record must point here), e.g. cp.example.com: " ""
           CP_DOMAIN="${REPLY,,}"
           ask "E-mail for Let's Encrypt certificates: " ""; ACME_EMAIL="$REPLY"
           ask "Restore a control plane backup (tarball, https URL or remote:path; empty = new): " ""
           CP_RESTORE="$REPLY" ;;
        2) ask "Control plane URL (https://...): " ""; CONTROL_PLANE="${REPLY%/}"
           ask "Registration token (Servers > Add server): " ""; TOKEN="$REPLY"
           ask "E-mail for Let's Encrypt certificates: " ""; ACME_EMAIL="$REPLY" ;;
        3) ;;
        *) die "invalid choice: $REPLY" ;;
    esac
    ask "Update the whole server (packages + kernel) first, and reboot at the end if needed? [Y/n]: " "y"
    case "${REPLY,,}" in n|no) OS_UPGRADE=0; REBOOT=no ;; esac
    exec 3<&-
fi

[[ -z "$CP_DOMAIN" || "$CP_DOMAIN" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] || die "invalid --with-control-plane domain: $CP_DOMAIN"
[[ -z "$CP_DOMAIN" || -z "$CONTROL_PLANE" ]] || die "--with-control-plane and --control-plane are mutually exclusive"
[[ -z "$CP_RESTORE" || -n "$CP_DOMAIN" ]] || die "--restore-control-plane needs --with-control-plane DOMAIN"
[[ "$CP_PORT" =~ ^[0-9]{2,5}$ ]] || die "invalid --control-plane-port"
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

# --------------------------------------------------------------------------- 2. OS upgrade (first)
BOOT_DIR="${NEXAPLOY_BOOT_DIR:-/boot}"                       # overridable for tests
REBOOT_REQUIRED_FILE="${NEXAPLOY_REBOOT_REQUIRED_FILE:-/var/run/reboot-required}"
APT_OPTS=(-y -qq -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
          -o DPkg::Lock::Timeout=600)

os_upgrade() {
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1
    local kernel_before; kernel_before="$(uname -r)"
    log "updating the whole server first (apt full-upgrade, kernel included); this can take a few minutes"
    apt-get update -qq -o DPkg::Lock::Timeout=600
    apt-get "${APT_OPTS[@]}" full-upgrade >/dev/null || die "system upgrade failed (apt-get full-upgrade)"
    if [[ $HWE_KERNEL -eq 1 ]]; then
        . /etc/os-release
        if [[ "${ID:-}" == ubuntu ]]; then
            log "installing the HWE kernel (linux-generic-hwe-${VERSION_ID})"
            apt-get "${APT_OPTS[@]}" install --install-recommends "linux-generic-hwe-${VERSION_ID}" >/dev/null \
                || warn "HWE kernel not available for Ubuntu ${VERSION_ID}; keeping the GA kernel"
        else
            warn "--hwe-kernel only applies to Ubuntu; skipped"
        fi
    fi
    apt-get "${APT_OPTS[@]}" autoremove --purge >/dev/null || true
    apt-get clean
    local newest
    newest="$(ls -1 "$BOOT_DIR"/vmlinuz-* 2>/dev/null | sed "s|^$BOOT_DIR/vmlinuz-||" | sort -V | tail -1)"
    if [[ -f "$REBOOT_REQUIRED_FILE" ]] || [[ -n "$newest" && "$newest" != "$kernel_before" ]]; then
        NEEDS_REBOOT=1
        log "server updated; running kernel $kernel_before, newest installed ${newest:-?} (reboot required)"
    else
        log "server updated; kernel $kernel_before is current"
    fi
}

finish_reboot() {
    [[ "${NEEDS_REBOOT:-0}" -eq 1 ]] || return 0
    if [[ "$REBOOT" == no ]]; then
        warn "a reboot is required to finish the system upgrade (new kernel): run 'sudo reboot' when convenient"
        return 0
    fi
    echo
    log "rebooting in 1 minute to load the new kernel (cancel with: sudo shutdown -c)"
    echo "  Benches, sites, the proxy, the database and the agent start again by themselves."
    shutdown -r +1 "Nexaploy: reboot to finish the system upgrade" >/dev/null 2>&1 \
        || warn "could not schedule the reboot; run 'sudo reboot'"
}

# --------------------------------------------------------------------------- 2b. DNS (all-in-one)
# Checked before the long system upgrade so a missing A record is fixed while the install runs.
DNS_WAIT_INTERVAL="${NEXAPLOY_DNS_WAIT_INTERVAL:-20}"
DNS_WAIT_TRIES="${NEXAPLOY_DNS_WAIT_TRIES:-30}"

can_ask() { [[ $ASSUME_YES -eq 0 ]] && ( : </dev/tty ) 2>/dev/null; }

tty_ask() {  # prompt default -> REPLY
    local answer
    printf '%s' "$1" >/dev/tty
    IFS= read -r answer </dev/tty || answer=""
    REPLY="${answer:-$2}"
}

is_public_ip() {  # 0 when globally routable (not private, CGNAT, loopback, link-local or ULA)
    case "$1" in
        ""|0.*|10.*|127.*|169.254.*|192.168.*) return 1 ;;
        172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 1 ;;
        100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) return 1 ;;
        ::1|[fF][eE]80:*|[fF][cCdD]*) return 1 ;;
    esac
    return 0
}

route_src() {  # destination -> source address the kernel would use
    ip route get "$1" 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}'
}

server_public_ips() {  # this server's addresses, one per line (ipify when behind NAT)
    local ips=() ip public=0
    ip="$(route_src 1.1.1.1)"; [[ -n "$ip" ]] && ips+=("$ip")
    ip="$(route_src 2606:4700:4700::1111)"; [[ -n "$ip" ]] && ips+=("$ip")
    for ip in "${ips[@]}"; do is_public_ip "$ip" && public=1; done
    if [[ $public -eq 0 ]] && command -v curl >/dev/null; then
        ip="$(curl -fsS -4 --max-time 5 https://api.ipify.org 2>/dev/null || true)"
        [[ "$ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && ips+=("$ip")
        ip="$(curl -fsS -6 --max-time 5 https://api6.ipify.org 2>/dev/null || true)"
        [[ "$ip" == *:* ]] && ips+=("$ip")
    fi
    [[ ${#ips[@]} -gt 0 ]] && printf '%s\n' "${ips[@]}" | awk 'NF && !seen[$0]++'
    return 0
}

domain_ips() {  # domain -> A/AAAA records, one per line (public resolver 1.1.1.1 when dig exists)
    local out=""
    if command -v dig >/dev/null; then
        out="$( { dig +short +time=3 +tries=1 A "$1" @1.1.1.1; dig +short +time=3 +tries=1 AAAA "$1" @1.1.1.1; } 2>/dev/null \
            | grep -E '^([0-9]+(\.[0-9]+){3}|[0-9a-fA-F:]*:[0-9a-fA-F:]*)$' || true)"
    fi
    [[ -n "$out" ]] || out="$(getent ahosts "$1" 2>/dev/null | awk '{print $1}' || true)"
    [[ -n "$out" ]] && printf '%s\n' "$out" | awk 'NF && !seen[$0]++'
    return 0
}

dns_verdict() {  # "resolved" "mine" (newline lists) -> ok | missing | elsewhere | partial
    local resolved=$1 mine=$2 ip hit=0 miss=0
    [[ -n "${resolved//[[:space:]]/}" ]] || { echo missing; return 0; }
    while read -r ip; do
        [[ -n "$ip" ]] || continue
        if grep -qxF -- "$ip" <<<"$mine"; then hit=$((hit + 1)); else miss=$((miss + 1)); fi
    done <<<"$resolved"
    if (( hit > 0 && miss == 0 )); then echo ok
    elif (( hit > 0 )); then echo partial
    else echo elsewhere; fi
}

dns_explain() {  # verdict resolved mine
    local verdict=$1 resolved=$2 mine=$3 ip v4="" v6=""
    while read -r ip; do
        [[ -n "$ip" ]] && is_public_ip "$ip" || continue
        if [[ "$ip" == *:* ]]; then v6="${v6:-$ip}"; else v4="${v4:-$ip}"; fi
    done <<<"$mine"
    echo >&2
    case "$verdict" in
        missing)   warn "$CP_DOMAIN does not resolve (no DNS record yet)" ;;
        elsewhere) warn "$CP_DOMAIN points to $(echo $resolved), not to this server" ;;
        partial)   warn "$CP_DOMAIN has records pointing elsewhere too: $(echo $resolved)" ;;
    esac
    {
        echo "  this server:  $(echo $mine)"
        echo "  Create this record at your DNS provider (TTL 300 is fine):"
        echo "    $CP_DOMAIN.   A      ${v4:-<public IPv4 of this server>}"
        [[ -n "$v6" ]] && echo "    $CP_DOMAIN.   AAAA   $v6   (optional)"
        [[ "$verdict" != missing ]] && echo "  and delete the A/AAAA records of $CP_DOMAIN that point to other addresses."
        echo "  Behind Cloudflare: use 'DNS only' (grey cloud) so Let's Encrypt can reach this server."
        echo "  Ports 80 and 443 must be open to the internet (firewall / cloud security group)."
    } >&2
}

dns_wait() {  # mine -> 0 once DOMAIN points here
    local i
    log "waiting for $CP_DOMAIN to point to this server (every ${DNS_WAIT_INTERVAL}s, up to $((DNS_WAIT_INTERVAL * DNS_WAIT_TRIES / 60)) min; Ctrl-C to stop)"
    for ((i = 1; i <= DNS_WAIT_TRIES; i++)); do
        sleep "$DNS_WAIT_INTERVAL"
        [[ "$(dns_verdict "$(domain_ips "$CP_DOMAIN")" "$1")" == ok ]] && return 0
    done
    warn "$CP_DOMAIN still does not point to this server"
    return 1
}

dns_check() {
    [[ -n "$CP_DOMAIN" ]] || return 0
    if [[ $SKIP_DNS_CHECK -eq 1 ]]; then
        warn "skipping the DNS check (--skip-dns-check): $CP_DOMAIN must point to this server for its certificate"
        return 0
    fi
    local mine resolved verdict
    mine="$(server_public_ips)"
    if [[ -z "$mine" ]]; then
        warn "cannot find this server's public IP; skipping the DNS check ($CP_DOMAIN must point here)"
        return 0
    fi
    while true; do
        resolved="$(domain_ips "$CP_DOMAIN")"
        verdict="$(dns_verdict "$resolved" "$mine")"
        if [[ "$verdict" == ok ]]; then
            log "DNS: $CP_DOMAIN -> $(echo $resolved) (this server). Ports 80 and 443 must be open to the internet."
            return 0
        fi
        dns_explain "$verdict" "$resolved" "$mine"
        can_ask || die "DNS of $CP_DOMAIN does not point to this server: create the record above and run again, or pass --skip-dns-check"
        tty_ask "[w]ait until it points here, [r]e-check now, [c]ontinue anyway, [a]bort? [w]: " w
        case "${REPLY,,}" in
            w*) dns_wait "$mine" && { log "DNS: $CP_DOMAIN now points to this server"; return 0; } ;;
            r*) ;;
            c*) warn "continuing: the TLS certificate of $CP_DOMAIN is issued once DNS points here"; return 0 ;;
            *)  die "aborted" ;;
        esac
    done
}

# --------------------------------------------------------------------------- 3. hardening
ssh_ports() {  # ports sshd listens on (+ the one of the current session), default 22
    local ports=""
    if command -v sshd >/dev/null 2>&1; then
        ports="$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' | tr '\n' ' ')"
    fi
    if [[ -n "${SSH_CONNECTION:-}" ]]; then ports="$ports $(awk '{print $4}' <<<"$SSH_CONNECTION")"; fi
    ports="$(tr ' ' '\n' <<<"$ports" | grep -E '^[0-9]+$' | sort -un | tr '\n' ' ')"
    echo "${ports:-22}"
}

configure_firewall() {
    [[ $FIREWALL -eq 1 ]] || { log "skipping the firewall (--no-firewall)"; return 0; }
    apt-get "${APT_OPTS[@]}" install ufw >/dev/null || { warn "could not install ufw; firewall not configured"; return 0; }
    local port https_port http_port
    http_port="$(sed -n 's/^HTTP_PORT=//p' "$ROOT/proxy/.env" 2>/dev/null)"; http_port="${http_port:-80}"
    https_port="$(sed -n 's/^HTTPS_PORT=//p' "$ROOT/proxy/.env" 2>/dev/null)"; https_port="${https_port:-443}"
    # SSH first, so enabling the firewall can never lock out the current session
    for port in $(ssh_ports); do ufw allow "$port/tcp" comment "ssh" >/dev/null; done
    ufw allow "$http_port/tcp" comment "nexaploy http (ACME + redirect)" >/dev/null
    ufw allow "$https_port/tcp" comment "nexaploy https" >/dev/null
    if ufw status 2>/dev/null | grep -q "^Status: active"; then
        log "firewall already active: added SSH ($(ssh_ports | xargs)), $http_port and $https_port (existing rules kept)"
    else
        ufw default deny incoming >/dev/null
        ufw default allow outgoing >/dev/null
        ufw --force enable >/dev/null || { warn "could not enable ufw"; return 0; }
        log "firewall enabled: incoming only SSH ($(ssh_ports | xargs)), $http_port, $https_port"
    fi
    # Docker publishes 80/443 itself (iptables DOCKER chain); nothing else is published:
    # the DB has no ports, the agent API and the all-in-one control plane bind 127.0.0.1.
}

configure_fail2ban() {
    [[ $FAIL2BAN -eq 1 ]] || { log "skipping fail2ban (--no-fail2ban)"; return 0; }
    apt-get "${APT_OPTS[@]}" install fail2ban >/dev/null || { warn "could not install fail2ban"; return 0; }
    local f=/etc/fail2ban/jail.d/nexaploy.local
    if [[ ! -f "$f" ]]; then
        cat > "$f" <<JAIL
# Written by the Nexaploy installer (remove or edit freely; never overwritten).
[sshd]
enabled  = true
port     = $(ssh_ports | xargs | tr ' ' ',')
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
JAIL
    fi
    systemctl enable --now fail2ban >/dev/null 2>&1 && systemctl restart fail2ban >/dev/null 2>&1 \
        && log "fail2ban protects SSH (5 failures in 10 min -> 1 h ban)" || warn "fail2ban did not start"
}

# --------------------------------------------------------------------------- 3b. packages
install_packages() {
    export DEBIAN_FRONTEND=noninteractive
    log "installing packages"
    apt-get update -qq
    apt-get "${APT_OPTS[@]}" install ca-certificates curl gnupg git jq acl rclone openssl iproute2 fuse3 unzip >/dev/null
    ensure_rclone

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

# rclone >= 1.62 (offsite attachments: IDrive e2 / Cloudflare R2 providers, VFS cache fixes);
# Ubuntu 22.04 ships 1.53. The official installer verifies the download checksum.
ensure_rclone() {
    local ver
    ver="$(rclone version 2>/dev/null | awk 'NR==1 {sub(/^v/, "", $2); print $2}')"
    if [[ -n "$ver" ]] && printf '1.62\n%s\n' "$ver" | sort -V -C; then
        return 0
    fi
    log "installing rclone >= 1.62 (found ${ver:-none})"
    curl -fsSL https://rclone.org/install.sh | bash >/dev/null || warn "rclone upgrade failed; offsite attachments need rclone >= 1.62"
}

# --------------------------------------------------------------------------- 4. users + tree
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
    mkdir_owned "$ROOT/storage" 0 0 0755
    # Per-site rclone mounts (offsite attachments) reach bench containers through mount
    # propagation; systemd hosts have / shared already, others need it once per boot.
    if [[ "$(findmnt -no PROPAGATION / 2>/dev/null)" == private ]]; then
        mount --make-rshared / 2>/dev/null || warn "/ is not a shared mount; offsite attachments will not work"
    fi
}

# --------------------------------------------------------------------------- 5. networks
create_networks() {
    local net
    for net in proxy-network nexaploy-db; do
        docker network inspect "$net" >/dev/null 2>&1 || { log "creating docker network $net"; docker network create --driver bridge "$net" >/dev/null; }
    done
}

# --------------------------------------------------------------------------- 6. agent
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
        if [[ "$src" != "$SRC_DIR" ]]; then
        log "copying agent source from $src"
        rm -rf "$SRC_DIR.new"
        mkdir -p "$SRC_DIR.new"
        tar -C "$src" --exclude=.venv --exclude=.git --exclude='__pycache__' --exclude='*.egg-info' -cf - . \
            | tar -C "$SRC_DIR.new" -xf -
        rm -rf "$SRC_DIR"; mv "$SRC_DIR.new" "$SRC_DIR"
        fi  # else: already running from the installed source (PKG_DIR/REPO_DIR set below)
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
    add_missing_env "$tpl/db/env.example" "$ROOT/db/.env"   # e.g. binlog settings (PITR) on upgrades
    set_env_var "$ROOT/db/.env" DB_ENV_FILE "$ROOT/config/db.env"

    install -m 0644 "$tpl/proxy/docker-compose.yml" "$ROOT/proxy/docker-compose.yml"
    if [[ -d "$tpl/proxy/config/traefik" ]]; then
        cp -a "$tpl/proxy/config/traefik/." "$ROOT/proxy/config/traefik/"
    fi
    # Traefik's file provider does not recurse: the shared middlewares must sit next to
    # the per-site route files (the agent ignores names that are not site names).
    install -m 0644 "$ROOT/proxy/config/traefik/dynamic.yml" "$ROOT/proxy/config/sites/_nexaploy-middlewares.yml"
    # remove the Docker-provider socket proxy of older installs
    docker rm -f nexaploy-socket-proxy >/dev/null 2>&1 || true
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
    if [[ $SKIP_FRAPPE_DOCKER -eq 0 && ! -d "$ROOT/images/frappe_docker/.git" ]]; then
        log "cloning frappe_docker ($FRAPPE_DOCKER_REF)"
        git clone --quiet "$FRAPPE_DOCKER_URL" "$ROOT/images/frappe_docker"
        git -C "$ROOT/images/frappe_docker" checkout --quiet "$FRAPPE_DOCKER_REF"
    fi
}

add_missing_env() {  # example-file env-file: append keys the env file does not set yet
    local example=$1 file=$2 line key
    while IFS= read -r line; do
        [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)= ]] || continue
        key="${BASH_REMATCH[1]}"
        grep -q "^$key=" "$file" || { printf '%s\n' "$line" >> "$file"; log "db/.env: added $key"; }
    done < "$example"
}

set_env_var() {  # file key value
    local file=$1 key=$2 value=$3 tmp
    tmp="$(mktemp)"
    awk -v k="$key" -v v="$value" 'BEGIN{done=0} $0 ~ "^"k"=" {print k"="v; done=1; next} {print} END{if(!done) print k"="v}' "$file" > "$tmp"
    cat "$tmp" > "$file"; rm -f "$tmp"
}

# --------------------------------------------------------------------------- 7. config
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
    # The server is now Active on the control plane, which queues server.configure_backups
    # (rclone.conf + [backup] in agent.toml); the worker picks it up on its first poll.
    log "backup storage will be configured by the control plane (job server.configure_backups)"
}

# --------------------------------------------------------------------------- 8. services
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

# --------------------------------------------------------------------------- 9. all-in-one
cp_exec() {  # bench command inside the control plane backend container
    docker exec "$CP_BENCH-backend" bench --site "$CP_DOMAIN" "$@"
}

agent_version() {
    "$VENV/bin/nexaploy-agent" version 2>/dev/null \
        || sed -n 's/^__version__ = "\(.*\)"/\1/p' "${PKG_DIR:-.}/nexaploy_agent/__init__.py" 2>/dev/null || true
}

cp_image_candidates() {  # agent-version -> published control plane images, in the order tried
    [[ -n "${1:-}" ]] && echo "$CP_IMAGE_REPO:$1"
    echo "$CP_IMAGE_REPO:$CP_FRAPPE_MAJOR"
    echo "$CP_IMAGE_REPO:latest"
}

cp_local_tag() { echo "nexaploy/control-plane:${1:-0}-local"; }

official_image() {  # pinned frappe/erpnext image the control plane is built from
    local f="${REPO_DIR:-}/image-builder/versions.json" img=""
    if [[ -f "$f" ]]; then
        img="$("${PYTHON:-python3}" -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], {}).get("official_image", ""))' \
            "$f" "$CP_FRAPPE_MAJOR" 2>/dev/null || true)"
    fi
    echo "${img:-$CP_OFFICIAL_FALLBACK}"
}

resolve_cp_image() {  # -> CP_IMAGE (zero flags: published image, else built on this server)
    if [[ -n "$CP_IMAGE" ]]; then
        docker image inspect "$CP_IMAGE" >/dev/null 2>&1 && return 0
        log "pulling control plane image $CP_IMAGE"
        docker pull "$CP_IMAGE" && return 0
        die "cannot pull $CP_IMAGE (check the name and registry access, or leave out --control-plane-image to use the published image or build one here)"
    fi
    local ver img
    ver="$(agent_version)"
    for img in $(cp_image_candidates "$ver") "$(cp_local_tag "$ver")"; do
        if docker image inspect "$img" >/dev/null 2>&1; then
            CP_IMAGE="$img"; log "control plane image $img (already on this server)"; return 0
        fi
    done
    for img in $(cp_image_candidates "$ver"); do
        log "pulling control plane image $img"
        if docker pull "$img" 2>/dev/null; then
            CP_IMAGE="$img"; log "control plane image $img"; return 0
        fi
        echo "  $img is not available"
    done
    build_cp_image "$ver"
}

build_cp_image() {  # agent-version: control-plane/docker/build.sh --from-official <pinned erpnext>
    local tag official start build="$REPO_DIR/control-plane/docker/build.sh"
    tag="$(cp_local_tag "$1")"
    [[ -f "$build" && -f "$REPO_DIR/e2e/build-test-image.sh" ]] \
        || die "no published control plane image ($CP_IMAGE_REPO) and no build script in $REPO_DIR; pass --control-plane-image"
    official="$(official_image)"
    warn "no published control plane image could be pulled; building $tag on this server instead"
    echo "  This takes about 5-15 minutes (about 2 GB to download), once. Later updates use"
    echo "  published images: Administration > Overview > Update control plane."
    start=$SECONDS
    log "step 1/2: pulling the official ERPNext image $official"
    if ! docker pull "$official"; then
        warn "cannot pull $official; trying frappe/erpnext:v$CP_FRAPPE_MAJOR"
        official="frappe/erpnext:v$CP_FRAPPE_MAJOR"
        docker pull "$official" || die "cannot pull $official (network or Docker Hub rate limit?); pass --control-plane-image"
    fi
    log "step 2/2: building $tag from $official (adds the nexaploy app; a few minutes)"
    bash "$build" --from-official "$official" --tag "$tag" >/dev/null \
        || die "building the control plane image failed; re-run install.sh, or pass --control-plane-image"
    log "built $tag in $(( (SECONDS - start) / 60 )) min $(( (SECONDS - start) % 60 )) s"
    CP_IMAGE="$tag"
}

restore_control_plane() {  # admin-password-or-empty: the control plane site from a backup
    local out ts
    log "restoring the control plane $CP_DOMAIN from $CP_RESTORE"
    out="$(nexaploy controlplane import-backup "$CP_RESTORE" --bench "$CP_BENCH" --site "$CP_DOMAIN")" \
        || die "cannot read the control plane backup $CP_RESTORE"
    ts="$(printf '%s\n' "$out" | tail -n 1)"
    [[ "$ts" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || die "unexpected result of the backup import: $out"
    nexaploy site restore "$CP_DOMAIN" --backup-id "$ts" --bench "$CP_BENCH" --wait \
        || die "restoring $CP_DOMAIN failed (nexaploy jobs; nexaploy retry <job>)"
    if [[ -n "${1:-}" ]]; then
        cp_exec set-admin-password "$1" >/dev/null || warn "could not set the Administrator password"
    fi
    CP_RESTORED=1
}

install_control_plane() {
    [[ -n "$CP_DOMAIN" ]] || return 0
    local api_ok=0 i
    for i in $(seq 1 30); do nexaploy ping >/dev/null 2>&1 && { api_ok=1; break; }; sleep 1; done
    [[ $api_ok -eq 1 ]] || die "agent API is not running (with --no-systemd start 'nexaploy-agent api' and 'worker' first)"

    local env_file="$ROOT/config/control-plane.env" pw_arg="$CP_ADMIN_PASSWORD" restored="" site_exists=0
    if [[ -f "$env_file" ]]; then
        # shellcheck disable=SC1090
        . "$env_file"
        CP_ADMIN_PASSWORD="${CP_ADMIN_PASSWORD:-${ADMIN_PASSWORD:-}}"
        CP_IMAGE="${CP_IMAGE:-${IMAGE:-}}"   # a re-run keeps the image the control plane runs
        restored="${RESTORED:-}"
    fi
    if nexaploy --json site list 2>/dev/null | grep -q "\"name\": *\"$CP_DOMAIN\""; then
        site_exists=1
    elif [[ -n "$CP_RESTORE" ]]; then
        restored=1
    fi
    if [[ -n "$restored" && -z "$pw_arg" ]]; then
        CP_ADMIN_PASSWORD=""   # the restored database keeps its own Administrator password
    else
        CP_ADMIN_PASSWORD="${CP_ADMIN_PASSWORD:-$(openssl rand -base64 18 | tr -d '/+=')}"
    fi
    resolve_cp_image
    ( umask 077; printf 'DOMAIN=%s\nBENCH=%s\nIMAGE=%s\nPORT=%s\nADMIN_PASSWORD=%s\nRESTORED=%s\n' \
        "$CP_DOMAIN" "$CP_BENCH" "$CP_IMAGE" "$CP_PORT" "$CP_ADMIN_PASSWORD" "$restored" > "$env_file" )

    if ! nexaploy --json bench list 2>/dev/null | grep -q "\"name\": *\"$CP_BENCH\""; then
        log "creating control plane bench $CP_BENCH"
        nexaploy bench create "$CP_BENCH" --image "$CP_IMAGE" --wait || die "bench $CP_BENCH could not be created (nexaploy jobs; nexaploy retry <job>)"
    fi
    # The agent (on the host) reaches the control plane on 127.0.0.1:$CP_PORT; docker compose
    # merges this override with the agent-rendered docker-compose.yml on every `up`.
    cat > "$ROOT/benches/$CP_BENCH/docker-compose.override.yml" <<YAML
# Written by install.sh (all-in-one): local port for the agent -> control plane link.
services:
  frontend:
    ports:
      - "127.0.0.1:$CP_PORT:8080"
YAML
    nexaploy bench start "$CP_BENCH" --wait >/dev/null || die "bench $CP_BENCH did not start"

    if [[ $site_exists -eq 0 && -n "$CP_RESTORE" ]]; then
        restore_control_plane "$pw_arg"
    elif [[ $site_exists -eq 0 ]]; then
        log "creating control plane site $CP_DOMAIN"
        NEXAPLOY_ADMIN_PASSWORD="$CP_ADMIN_PASSWORD" nexaploy site create "$CP_DOMAIN" --bench "$CP_BENCH" \
            --app nexaploy --wait || die "site $CP_DOMAIN could not be created"
    elif [[ -n "$CP_RESTORE" ]]; then
        warn "site $CP_DOMAIN already exists; --restore-control-plane ignored (to replace it: nexaploy controlplane import-backup SRC, then nexaploy site restore $CP_DOMAIN --backup-id <id>)"
    fi
    cp_exec set-config host_name "https://$CP_DOMAIN" >/dev/null
    cp_exec enable-scheduler >/dev/null || true

    local registered
    registered="$("$VENV/bin/python" -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb")).get("control_plane",{}).get("token",""))' "$CONFIG")"
    if [[ -z "$registered" || $FORCE_REGISTER -eq 1 ]]; then
        log "registering this server with its own control plane"
        local out token
        out="$(cp_exec execute nexaploy.all_in_one.bootstrap)" || die "control plane bootstrap failed"
        token="$(printf '%s\n' "$out" | "$VENV/bin/python" -c '
import json, sys
for line in reversed(sys.stdin.read().splitlines()):
    line = line.strip()
    if line.startswith("{") and "nexaploy_bootstrap" in line:
        print(json.loads(line)["nexaploy_bootstrap"]["token"]); break
')"
        [[ -n "$token" ]] || die "no registration token from the control plane bootstrap"
        local i ok=0
        for i in $(seq 1 30); do
            curl -fsS -o /dev/null -H "X-Frappe-Site-Name: $CP_DOMAIN" "http://127.0.0.1:$CP_PORT/api/method/ping" && { ok=1; break; }
            sleep 2
        done
        [[ $ok -eq 1 ]] || die "control plane does not answer on 127.0.0.1:$CP_PORT"
        "$VENV/bin/nexaploy-agent" register --control-plane "http://127.0.0.1:$CP_PORT" --token "$token" \
            --site-name "$CP_DOMAIN" --config "$CONFIG"
        CONTROL_PLANE="https://$CP_DOMAIN (all-in-one)"
    fi
    local server_id
    server_id="$("$VENV/bin/python" -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["agent"]["server_id"])' "$CONFIG")"
    cp_exec execute nexaploy.all_in_one.mark_host \
        --kwargs "{\"server_id\": \"$server_id\", \"bench\": \"$CP_BENCH\", \"site\": \"$CP_DOMAIN\"}" >/dev/null \
        || warn "could not flag $server_id as the control plane host"
    if [[ $NO_SYSTEMD -eq 0 ]]; then
        systemctl restart nexaploy-agent-worker   # pick up the control plane credentials
    else
        warn "restart 'nexaploy-agent worker' to connect it to the control plane"
    fi
    CP_SUMMARY=1
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
    if [[ "${CP_SUMMARY:-0}" -eq 1 ]]; then
        echo
        log "Control plane: https://$CP_DOMAIN"
        echo "  login:    Administrator"
        if [[ "${CP_RESTORED:-0}" -eq 1 && -z "$CP_ADMIN_PASSWORD" ]]; then
            echo "  password: unchanged (the one of the restored control plane)"
        else
            echo "  password: stored in $ROOT/config/control-plane.env (ADMIN_PASSWORD)"
        fi
        echo "  image:    $CP_IMAGE"
        echo "  DNS:      $CP_DOMAIN must point to this server for the TLS certificate"
        echo "  next:     open https://$CP_DOMAIN/dashboard/admin and follow the setup checklist"
        if [[ "${CP_RESTORED:-0}" -eq 1 ]]; then
            echo
            log "Restored from $CP_RESTORE"
            echo "  1. Point the DNS record of $CP_DOMAIN to this server (other servers' agents talk to"
            echo "     https://$CP_DOMAIN; their tokens are in the restored database and keep working)."
            echo "  2. If the old control plane server still runs: stop its control plane bench"
            echo "     (nexaploy bench stop $CP_BENCH) and point its agent here:"
            echo "     nexaploy controlplane repoint --url https://$CP_DOMAIN && systemctl restart nexaploy-agent-worker"
        fi
    fi
}

main() {
    preflight
    dns_check
    if [[ $SKIP_PACKAGES -eq 1 ]]; then
        log "skipping OS packages (--skip-packages)"
        command -v docker >/dev/null || die "docker not found"
        docker compose version >/dev/null 2>&1 || die "docker compose plugin not found"
        PYTHON=""
        for candidate in python3.13 python3.12 python3.11 python3; do
            if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
                PYTHON="$(command -v "$candidate")"; break
            fi
        done
        [[ -n "$PYTHON" ]] || die "python >= 3.11 not found"
    else
        [[ $OS_UPGRADE -eq 1 ]] && os_upgrade || log "skipping the system upgrade (--no-os-upgrade)"
        install_packages
    fi
    create_tree
    create_networks
    install_agent
    install_stacks
    write_config
    register
    if [[ $NO_SYSTEMD -eq 1 ]]; then
        log "skipping systemd units (--no-systemd): run 'NEXAPLOY_CONFIG=$CONFIG nexaploy-agent api' and '... worker'"
    else
        install_units
    fi
    if [[ $SKIP_PACKAGES -eq 0 ]]; then
        configure_firewall
        configure_fail2ban
    fi
    start_stacks
    install_control_plane
    summary
    finish_reboot
}

[[ "${NEXAPLOY_INSTALL_SOURCE_ONLY:-0}" == 1 ]] || main "$@"
