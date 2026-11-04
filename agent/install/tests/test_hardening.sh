#!/usr/bin/env bash
# install.sh: firewall (ufw) and fail2ban with stubbed commands.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../install.sh"
fail() { echo "FAIL: $*" >&2; exit 1; }

setup() {  # ufw-active(0/1) sshd-ports
    T="$(mktemp -d)"; mkdir -p "$T/bin" "$T/root/proxy" "$T/etc"
    printf 'HTTP_PORT=80\nHTTPS_PORT=8443\n' > "$T/root/proxy/.env"
    for c in apt-get systemctl; do printf '#!/bin/bash\necho "%s $*" >> %s/calls\n' "$c" "$T" > "$T/bin/$c"; done
    cat > "$T/bin/ufw" <<STUB
#!/bin/bash
echo "ufw \$*" >> "$T/calls"
if [[ "\$1" == status ]]; then [[ "$1" == 1 ]] && echo "Status: active" || echo "Status: inactive"; fi
STUB
    printf '#!/bin/bash\nfor p in %s; do echo "port $p"; done\n' "$2" > "$T/bin/sshd"
    chmod +x "$T/bin/"*; : > "$T/calls"
}

run() {
    PATH="$T/bin:$PATH" NEXAPLOY_INSTALL_SOURCE_ONLY=1 SSH_CONNECTION="${SSHC:-}" \
        bash -c "set -euo pipefail; source '$SCRIPT' --skip-preflight --root '$T/root' $ARGS </dev/null; $1" > "$T/out" 2>&1
}

# 1. inactive ufw: SSH ports (sshd + current session) allowed BEFORE enable, deny incoming default
setup 0 "22 2222"; ARGS=""; SSHC="1.2.3.4 50000 5.6.7.8 2200"
run 'configure_firewall'
for p in 22 2222 2200 80 8443; do grep -q "ufw allow $p/tcp" "$T/calls" || fail "port $p not allowed"; done
allow_line=$(grep -n "ufw allow 22/tcp" "$T/calls" | cut -d: -f1); enable_line=$(grep -n "ufw --force enable" "$T/calls" | cut -d: -f1)
[[ -n "$enable_line" && "$allow_line" -lt "$enable_line" ]] || fail "ssh must be allowed before enabling"
grep -q "ufw default deny incoming" "$T/calls" || fail "default deny missing"

# 2. active ufw: rules added, no reset/defaults/enable
setup 1 "22"; ARGS=""; SSHC=""
run 'configure_firewall'
grep -q "ufw --force enable\|ufw default" "$T/calls" && fail "touched an active firewall's defaults"
grep -q "ufw allow 443\|ufw allow 8443/tcp" "$T/calls" || fail "https not allowed on active firewall"

# 3. --no-firewall / --no-fail2ban
setup 0 "22"; ARGS="--no-firewall --no-fail2ban"; SSHC=""
run 'configure_firewall; configure_fail2ban'
grep -q "ufw\|fail2ban" "$T/calls" && fail "ran despite --no-firewall/--no-fail2ban"

echo "all hardening tests passed"
