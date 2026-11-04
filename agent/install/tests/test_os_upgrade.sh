#!/usr/bin/env bash
# install.sh: the system upgrade runs first and the reboot decision (stubbed apt-get/uname/shutdown).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../install.sh"
fail() { echo "FAIL: $*" >&2; exit 1; }

setup() {  # running-kernel newest-kernel reboot-flag(0/1)
    T="$(mktemp -d)"; mkdir -p "$T/bin" "$T/boot"
    cat > "$T/bin/apt-get" <<STUB
#!/bin/bash
echo "apt-get \$*" >> "$T/calls"
STUB
    printf '#!/bin/bash\necho %s\n' "$1" > "$T/bin/uname"
    printf '#!/bin/bash\necho "shutdown $*" >> %s/calls\n' "$T" > "$T/bin/shutdown"
    chmod +x "$T/bin/"*
    touch "$T/boot/vmlinuz-$1" "$T/boot/vmlinuz-$2"
    [[ "$3" == 1 ]] && touch "$T/reboot-required"
    : > "$T/calls"
}

run() {  # extra shell statements
    PATH="$T/bin:$PATH" NEXAPLOY_INSTALL_SOURCE_ONLY=1 NEXAPLOY_BOOT_DIR="$T/boot" \
    NEXAPLOY_REBOOT_REQUIRED_FILE="$T/reboot-required" \
        bash -c "set -euo pipefail; source '$SCRIPT' $ARGS </dev/null; $1" > "$T/out" 2>&1
}

# 1. new kernel -> full-upgrade (non-interactive, keep configs) and a reboot scheduled
setup 6.8.0-40-generic 6.8.0-49-generic 0; ARGS="--skip-preflight"
run 'os_upgrade; finish_reboot'
grep -q "full-upgrade" "$T/calls" || fail "full-upgrade not run"
grep -q -- "--force-confold" "$T/calls" || fail "config files not kept"
grep -q "autoremove" "$T/calls" || fail "autoremove not run"
grep -q "shutdown -r +1" "$T/calls" || fail "reboot not scheduled for a new kernel"
grep -q "reboot required" "$T/out" || fail "reboot requirement not reported"

# 2. --no-reboot -> warning, no shutdown
setup 6.8.0-40-generic 6.8.0-49-generic 1; ARGS="--skip-preflight --no-reboot"
run 'os_upgrade; finish_reboot'
grep -q "shutdown" "$T/calls" && fail "rebooted despite --no-reboot"
grep -q "sudo reboot" "$T/out" || fail "no reboot hint with --no-reboot"

# 3. current kernel, no flag file -> no reboot
setup 6.8.0-49-generic 6.8.0-49-generic 0; ARGS="--skip-preflight"
run 'os_upgrade; finish_reboot'
grep -q "shutdown" "$T/calls" && fail "rebooted without a new kernel"
grep -q "is current" "$T/out" || fail "current kernel not reported"

# 4. reboot-required flag file alone (e.g. libc) -> reboot
setup 6.8.0-49-generic 6.8.0-49-generic 1; ARGS="--skip-preflight"
run 'os_upgrade; finish_reboot'
grep -q "shutdown -r +1" "$T/calls" || fail "reboot-required file ignored"

# 5. upgrade runs before any package install in main()
grep -n "os_upgrade\|install_packages$" "$SCRIPT" | awk -F: '/os_upgrade \|\|/ {u=$1} /^[0-9]+:[[:space:]]+install_packages$/ {p=$1} END {exit !(u && p && u < p)}' \
    || fail "os_upgrade must run before install_packages"

# 6. --no-os-upgrade parses
setup 6.8.0-49-generic 6.8.0-49-generic 0; ARGS="--skip-preflight --no-os-upgrade --hwe-kernel"
run '[[ $OS_UPGRADE -eq 0 && $HWE_KERNEL -eq 1 ]]' || fail "flags not parsed"

echo "all os-upgrade tests passed"
