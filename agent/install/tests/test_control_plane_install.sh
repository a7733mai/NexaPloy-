#!/usr/bin/env bash
# install.sh all-in-one: control plane image resolution (published -> local build), DNS preflight,
# --restore-control-plane parsing (stubbed docker/dig/getent/ip/curl).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../install.sh"
fail() { echo "FAIL: $*" >&2; [[ -f "${T:-}/out" ]] && sed 's/^/  | /' "$T/out" >&2; exit 1; }
REPO=ghcr.io/a7733mai/nexaploy-control-plane

setup() {
    T="$(mktemp -d)"; mkdir -p "$T/bin" "$T/venv/bin" "$T/repo/control-plane/docker" "$T/repo/e2e" "$T/repo/image-builder"
    : > "$T/calls"; : > "$T/local"; : > "$T/pullable"
    cat > "$T/bin/docker" <<STUB
#!/bin/bash
echo "docker \$*" >> "$T/calls"
case "\$1 \$2" in
    "image inspect") grep -qxF -- "\${@: -1}" "$T/local" ;;
    "pull "*) grep -qxF -- "\$2" "$T/pullable" && echo "\$2" >> "$T/local" ;;
    *) exit 0 ;;
esac
STUB
    printf '#!/bin/bash\necho 0.3.0\n' > "$T/venv/bin/nexaploy-agent"
    cat > "$T/repo/control-plane/docker/build.sh" <<STUB
#!/bin/bash
echo "build.sh \$*" >> "$T/calls"
STUB
    touch "$T/repo/e2e/build-test-image.sh"
    printf '{"16": {"python": "3.14", "official_image": "frappe/erpnext:v16.99.0"}}\n' > "$T/repo/image-builder/versions.json"
    # DNS: this server is 203.0.113.10 unless $T/route says otherwise
    echo 203.0.113.10 > "$T/route"
    cat > "$T/bin/ip" <<STUB
#!/bin/bash
[[ "\$*" == "route get 1.1.1.1" ]] && echo "1.1.1.1 via 203.0.113.1 dev eth0 src \$(cat $T/route) uid 0"
exit 0
STUB
    cat > "$T/bin/dig" <<STUB
#!/bin/bash
echo "dig \$*" >> "$T/calls"
n=\$(cat "$T/dig_n" 2>/dev/null || echo 0); echo \$((n + 1)) > "$T/dig_n"
[[ " \$* " == *" A "* ]] || exit 0
if [[ -f "$T/dig_after" && \$n -ge 2 ]]; then cat "$T/dig_after"; else cat "$T/dig" 2>/dev/null; fi
exit 0
STUB
    printf '#!/bin/bash\necho "curl $*" >> %s/calls\ncat %s/ipify 2>/dev/null\n' "$T" "$T" > "$T/bin/curl"
    printf '#!/bin/bash\n[[ -f %s/getent ]] && cat %s/getent\nexit 0\n' "$T" "$T" > "$T/bin/getent"
    chmod +x "$T/bin/"* "$T/venv/bin/"* "$T/repo/control-plane/docker/build.sh"
}

run() {  # args, statements -> exit status in $RC
    set +e
    PATH="$T/bin:$PATH" NEXAPLOY_INSTALL_SOURCE_ONLY=1 NEXAPLOY_DNS_WAIT_INTERVAL=0 NEXAPLOY_DNS_WAIT_TRIES=3 \
        bash -c "set -euo pipefail; source '$SCRIPT' $1 </dev/null; VENV='$T/venv'; REPO_DIR='$T/repo'; PYTHON=python3; $2" \
        > "$T/out" 2>&1
    RC=$?
    set -e
}

# ---- image resolution -------------------------------------------------------------------------
# 1. candidates: <agent version>, :16, :latest
setup; run "" 'cp_image_candidates 0.3.0'
[[ "$(cat "$T/out")" == "$REPO:0.3.0"$'\n'"$REPO:16"$'\n'"$REPO:latest" ]] || fail "candidate order"
run "" 'cp_image_candidates ""'
[[ "$(head -1 "$T/out")" == "$REPO:16" ]] || fail "no version -> :16 first"

# 2. zero flags: the versioned image is missing, :16 is published -> :16, nothing built
setup; echo "$REPO:16" > "$T/pullable"
run "--with-control-plane cp.example.com" 'resolve_cp_image; echo "IMAGE=$CP_IMAGE"'
[[ $RC -eq 0 ]] || fail "resolve with :16 published"
grep -q "IMAGE=$REPO:16" "$T/out" || fail "expected $REPO:16"
grep -n "docker pull" "$T/calls" | head -1 | grep -q "$REPO:0.3.0" || fail "versioned tag must be tried first"
grep -q "build.sh" "$T/calls" && fail "built although an image was published"

# 3. nothing published -> built locally from the pinned official image (progress + estimate)
setup; echo "frappe/erpnext:v16.99.0" > "$T/pullable"
run "--with-control-plane cp.example.com" 'resolve_cp_image; echo "IMAGE=$CP_IMAGE"'
[[ $RC -eq 0 ]] || fail "local build path"
grep -q "IMAGE=nexaploy/control-plane:0.3.0-local" "$T/out" || fail "local tag"
grep -q "build.sh --from-official frappe/erpnext:v16.99.0 --tag nexaploy/control-plane:0.3.0-local" "$T/calls" || fail "build.sh args"
grep -q "5-15 minutes" "$T/out" || fail "time estimate"
grep -q "step 2/2" "$T/out" || fail "progress"
for tag in 0.3.0 16 latest; do grep -q "docker pull $REPO:$tag" "$T/calls" || fail "did not try :$tag"; done

# 4. pinned official tag missing -> frappe/erpnext:v16
setup; echo "frappe/erpnext:v16" > "$T/pullable"
run "--with-control-plane cp.example.com" 'resolve_cp_image; echo "IMAGE=$CP_IMAGE"'
[[ $RC -eq 0 ]] || fail "official fallback"
grep -q "build.sh --from-official frappe/erpnext:v16 " "$T/calls" || fail "fallback official image"

# 5. a re-run finds the locally built image (no pull at all)
setup; echo "nexaploy/control-plane:0.3.0-local" > "$T/local"
run "--with-control-plane cp.example.com" 'resolve_cp_image; echo "IMAGE=$CP_IMAGE"'
grep -q "IMAGE=nexaploy/control-plane:0.3.0-local" "$T/out" || fail "local image reused"
grep -q "docker pull" "$T/calls" && fail "pulled although present"

# 6. --control-plane-image is an override: used as is, never replaced by a build
setup; echo "registry.example.com/cp:9" > "$T/pullable"
run "--with-control-plane cp.example.com --control-plane-image registry.example.com/cp:9" 'resolve_cp_image; echo "IMAGE=$CP_IMAGE"'
grep -q "IMAGE=registry.example.com/cp:9" "$T/out" || fail "override"
setup
run "--with-control-plane cp.example.com --control-plane-image registry.example.com/cp:9" 'resolve_cp_image'
[[ $RC -ne 0 ]] || fail "unpullable override must fail"
grep -q "build.sh" "$T/calls" && fail "override must not build"

# 7. no versions.json -> the constant
setup; rm "$T/repo/image-builder/versions.json"
run "" 'official_image'
grep -qx "frappe/erpnext:v16.53.0" "$T/out" || fail "official image constant"

# ---- DNS preflight -----------------------------------------------------------------------------
# 8. verdicts
setup
run "" '[[ $(dns_verdict "1.2.3.4" "1.2.3.4") == ok ]] && [[ $(dns_verdict "" "1.2.3.4") == missing ]] &&
        [[ $(dns_verdict "5.6.7.8" "1.2.3.4") == elsewhere ]] && [[ $(dns_verdict $'"'"'1.2.3.4\n5.6.7.8'"'"' "1.2.3.4") == partial ]]'
[[ $RC -eq 0 ]] || fail "dns_verdict"
run "" 'is_public_ip 10.1.2.3 || is_public_ip 172.20.0.1 || is_public_ip 100.64.0.1 || is_public_ip fd00::1 || ! is_public_ip 203.0.113.10'
[[ $RC -ne 0 ]] || fail "is_public_ip"

# 9. domain points here (public resolver) -> ok, ports warning shown
setup; echo 203.0.113.10 > "$T/dig"
run "--with-control-plane cp.example.com --yes" 'dns_check'
[[ $RC -eq 0 ]] || fail "dns ok"
grep -q "dig +short +time=3 +tries=1 A cp.example.com @1.1.1.1" "$T/calls" || fail "dig @1.1.1.1 not used"
grep -q "80 and 443 must be open" "$T/out" || fail "ports reminder"

# 10. points elsewhere + --yes -> fail fast with the exact record to create
setup; echo 198.51.100.7 > "$T/dig"
run "--with-control-plane cp.example.com --yes" 'dns_check'
[[ $RC -ne 0 ]] || fail "--yes must fail on wrong DNS"
grep -q "points to 198.51.100.7" "$T/out" || fail "resolved address not shown"
grep -Eq "cp\.example\.com\. +A +203\.0\.113\.10" "$T/out" || fail "A record instruction"
grep -q -- "--skip-dns-check" "$T/out" || fail "skip hint"

# 11. --skip-dns-check
run "--with-control-plane cp.example.com --yes --skip-dns-check" 'dns_check'
[[ $RC -eq 0 ]] || fail "--skip-dns-check"

# 12. no record at all, dig absent -> getent fallback, "does not resolve"
setup; rm "$T/bin/dig"
run "--with-control-plane cp.example.com --yes" 'dns_check'
[[ $RC -ne 0 ]] && grep -q "does not resolve" "$T/out" || fail "missing record"
echo "203.0.113.10    STREAM cp.example.com" > "$T/getent"
run "--with-control-plane cp.example.com --yes" 'dns_check'
[[ $RC -eq 0 ]] || fail "getent fallback"

# 13. behind NAT (private route source) -> ipify
setup; echo 10.0.0.5 > "$T/route"; echo 203.0.113.10 > "$T/ipify"; echo 203.0.113.10 > "$T/dig"
run "--with-control-plane cp.example.com --yes" 'dns_check'
[[ $RC -eq 0 ]] || fail "NAT: ipify fallback"
grep -q "curl -fsS -4 --max-time 5 https://api.ipify.org" "$T/calls" || fail "ipify not queried"

# 14. interactive: continue anyway / wait until it points here
setup; echo 198.51.100.7 > "$T/dig"
run "--with-control-plane cp.example.com" 'can_ask() { return 0; }; tty_ask() { REPLY=c; }; dns_check'
[[ $RC -eq 0 ]] && grep -q "continuing" "$T/out" || fail "interactive continue"
setup; echo 198.51.100.7 > "$T/dig"; echo 203.0.113.10 > "$T/dig_after"
run "--with-control-plane cp.example.com" 'can_ask() { return 0; }; tty_ask() { REPLY=w; }; dns_check'
[[ $RC -eq 0 ]] && grep -q "now points to this server" "$T/out" || fail "interactive wait"
setup; echo 198.51.100.7 > "$T/dig"
run "--with-control-plane cp.example.com" 'can_ask() { return 0; }; tty_ask() { REPLY=a; }; dns_check'
[[ $RC -ne 0 ]] || fail "interactive abort"

# 15. only with --with-control-plane; dns_check runs right after preflight
setup; run "" 'dns_check'
[[ $RC -eq 0 && ! -s "$T/calls" ]] || fail "dns check without a control plane domain"
grep -A2 "^main() {" "$SCRIPT" | grep -q "dns_check" || fail "dns_check must run first in main()"

# ---- restore flags ------------------------------------------------------------------------------
setup
run "--with-control-plane cp.example.com --restore-control-plane /root/cp.tar" '[[ $CP_RESTORE == /root/cp.tar ]]'
[[ $RC -eq 0 ]] || fail "--restore-control-plane parse"
run "--restore-control-plane /root/cp.tar" 'true'
[[ $RC -ne 0 ]] && grep -q "needs --with-control-plane" "$T/out" || fail "restore without domain"

echo "all control-plane install tests passed"
