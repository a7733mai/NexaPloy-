#!/usr/bin/env bash
# Tests for packer/files/usr/local/sbin/nexaploy-firstboot (dry-run: prints the install.sh command).
#   bash packer/tests/test_firstboot.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FB="$HERE/../files/usr/local/sbin/nexaploy-firstboot"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export NEXAPLOY_FIRSTBOOT_MARKER="$TMP/done" NEXAPLOY_FIRSTBOOT_ENV="$TMP/none.env" NEXAPLOY_IMAGE_SRC=/src

fails=0
TOKEN=abcdefghijklmnop0123456789_-XYZ

check() {  # name expected-substring ...args   (expects exit 0)
    local name=$1 want=$2; shift 2
    local out
    if ! out="$(bash "$FB" --dry-run "$@" 2>&1)"; then
        echo "FAIL $name: non-zero exit ($out)"; fails=$((fails + 1)); return
    fi
    if [[ "$out" != *"$want"* ]]; then
        echo "FAIL $name: expected [$want] in [$out]"; fails=$((fails + 1)); return
    fi
    echo "ok   $name"
}

refuse() {  # name ...args   (expects a non-zero exit)
    local name=$1; shift
    if bash "$FB" --dry-run "$@" >/dev/null 2>&1; then
        echo "FAIL $name: accepted"; fails=$((fails + 1)); return
    fi
    echo "ok   $name"
}

ud() { printf '%s\n' "$@" > "$TMP/ud"; echo "$TMP/ud"; }

check "plain env user-data" "--control-plane https://cp.example.com --token $TOKEN" \
    --user-data "$(ud "NEXAPLOY_CONTROL_PLANE=https://cp.example.com/" "NEXAPLOY_TOKEN=$TOKEN")"
check "always local source, no apt" "--agent-source /src --skip-packages" \
    --user-data "$(ud "NEXAPLOY_CONTROL_PLANE=https://cp.example.com" "NEXAPLOY_TOKEN=$TOKEN")"
check "quoted + export + CRLF" "--control-plane https://cp.example.com:8443 --token $TOKEN --acme-email ops@example.com" \
    --user-data "$(ud "export NEXAPLOY_CONTROL_PLANE=\"https://cp.example.com:8443\""$'\r' \
                      "NEXAPLOY_TOKEN='$TOKEN'" "NEXAPLOY_ACME_EMAIL=ops@example.com  ")"
check "cloud-config comments" "--with-control-plane cloud.example.com" \
    --user-data "$(ud "#cloud-config" "# NEXAPLOY_DOMAIN=cloud.example.com" "packages: [htop]")"
check "all-in-one with email" "--with-control-plane erp.example.org --acme-email a@b.io" \
    --user-data "$(ud "NEXAPLOY_DOMAIN=erp.example.org" "NEXAPLOY_ACME_EMAIL=a@b.io")"
check "flags win over user-data" "--control-plane https://flag.example.com --token $TOKEN" \
    --control-plane https://flag.example.com --token "$TOKEN" \
    --user-data "$(ud "NEXAPLOY_CONTROL_PLANE=https://ud.example.com" "NEXAPLOY_TOKEN=$TOKEN")"
check "nothing configured" "nothing to do" --user-data "$(ud "#cloud-config" "runcmd: [echo hi]")"
check "other keys ignored" "nothing to do" --user-data "$(ud "NEXAPLOY_EVIL=\$(touch /tmp/x)" "PATH=/tmp")"

refuse "token without control plane" --user-data "$(ud "NEXAPLOY_TOKEN=$TOKEN")"
refuse "control plane without token" --user-data "$(ud "NEXAPLOY_CONTROL_PLANE=https://cp.example.com")"
refuse "bad url" --user-data "$(ud "NEXAPLOY_CONTROL_PLANE=https://cp.example.com/\$(id)" "NEXAPLOY_TOKEN=$TOKEN")"
refuse "file url" --user-data "$(ud "NEXAPLOY_CONTROL_PLANE=file:///etc/passwd" "NEXAPLOY_TOKEN=$TOKEN")"
refuse "bad token" --user-data "$(ud "NEXAPLOY_CONTROL_PLANE=https://cp.example.com" "NEXAPLOY_TOKEN=short;rm")"
refuse "bad domain" --user-data "$(ud "NEXAPLOY_DOMAIN=Not A Domain")"
refuse "domain and control plane" --user-data "$(ud "NEXAPLOY_DOMAIN=a.example.com" "NEXAPLOY_CONTROL_PLANE=https://cp.example.com" "NEXAPLOY_TOKEN=$TOKEN")"
refuse "bad email" --user-data "$(ud "NEXAPLOY_DOMAIN=a.example.com" "NEXAPLOY_ACME_EMAIL=x@y;reboot")"

date > "$NEXAPLOY_FIRSTBOOT_MARKER"
check "runs once" "already completed" --user-data "$(ud "NEXAPLOY_DOMAIN=a.example.com")"
check "--force runs again" "--with-control-plane a.example.com" --force --user-data "$(ud "NEXAPLOY_DOMAIN=a.example.com")"

if [[ $fails -gt 0 ]]; then
    echo "$fails test(s) failed"
    exit 1
fi
echo "all firstboot tests passed"
