#!/usr/bin/env bash
# shellcheck disable=SC2001,SC2015,SC2016
# Tests for image-builder/build.sh using --dry-run (no Docker needed).
# Usage: bash image-builder/tests/test_build_sh.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BUILD="$ROOT/build.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

export NEXAPLOY_BUILDER=buildx   # deterministic command shape
PASS=0 FAIL=0
GOOD_TAG="nexaploy/bench:erp-v15-main-20261103T2130"

ok()   { PASS=$((PASS+1)); echo "ok   - $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL - $1"; [[ -n "${2:-}" ]] && sed 's/^/       /' <<<"$2"; }

# expect_ok NAME ARGS... ; sets $OUT (stdout)
expect_ok() {
  local name="$1"; shift
  OUT="$("$BUILD" "$@" 2>"$TMP/err")"; local rc=$?
  if [[ $rc -eq 0 ]]; then ok "$name"; return 0; fi
  fail "$name (rc=$rc)" "$(cat "$TMP/err")"; return 1
}
# expect_fail NAME PATTERN ARGS...
expect_fail() {
  local name="$1" pat="$2"; shift 2
  "$BUILD" "$@" >"$TMP/out" 2>"$TMP/err"; local rc=$?
  if [[ $rc -ne 0 ]] && grep -Eq -- "$pat" "$TMP/err"; then ok "$name"
  else fail "$name (rc=$rc, expected stderr ~ /$pat/)" "$(cat "$TMP/out" "$TMP/err")"; fi
}
contains() { # NAME HAYSTACK NEEDLE
  if [[ "$2" == *"$3"* ]]; then ok "$1"; else fail "$1" "missing: $3"; fi
}
not_contains() {
  if [[ "$2" != *"$3"* ]]; then ok "$1"; else fail "$1" "unexpected: $3"; fi
}

# ------------------------------------------------------------------ static
bash -n "$BUILD" && ok "bash -n build.sh" || fail "bash -n build.sh"
for f in "$ROOT/versions.json" "$ROOT"/examples/apps-v1{4,5,6}.json; do
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$f" && ok "valid json $(basename "$f")" || fail "valid json $f"
done
expect_ok "--help exits 0" --help && contains "--help mentions --apps-json" "$OUT" "--apps-json"

# ------------------------------------------------------------------ happy paths
for m in 14 15 16; do
  if expect_ok "dry-run v$m" --major "$m" --apps-json "$ROOT/examples/apps-v$m.json" \
       --tag "nexaploy/bench:erp-v$m-main-20261103T2130" --context "$TMP/ctx" --dry-run; then
    last="$(tail -n1 <<<"$OUT")"
    [[ "$last" == "nexaploy/bench:erp-v$m-main-20261103T2130" ]] && ok "v$m last line is tag" || fail "v$m last line is tag" "$last"
    contains "v$m frappe branch" "$OUT" "FRAPPE_BRANCH=version-$m"
    contains "v$m major label"   "$OUT" "FRAPPE_MAJOR=$m"
    contains "v$m release group" "$OUT" "RELEASE_GROUP=erp-v$m-main"
    contains "v$m build id"      "$OUT" "BUILD_ID=20261103T2130"
    contains "v$m uid"           "$OUT" "FRAPPE_UID=3113"
    contains "v$m target"        "$OUT" "--target backend"
    contains "v$m clone hint"    "$OUT" "git clone --depth 1"
    b64="$(base64 <"$ROOT/examples/apps-v$m.json" | tr -d '\n')"
    contains "v$m apps base64"   "$OUT" "APPS_JSON_BASE64=$b64"
  fi
done

expect_ok "v15 defaults from versions.json" --major 15 --apps-json "$ROOT/examples/apps-v15.json" --tag "$GOOD_TAG" --context "$TMP/ctx" --dry-run
contains "v15 python default" "$OUT" "PYTHON_VERSION=3.11.6"
contains "v15 node default"   "$OUT" "NODE_VERSION=18.20.2"
contains "v15 debian default" "$OUT" "DEBIAN_BASE=bookworm"
contains "--load without push" "$OUT" "--load"

expect_ok "overrides" --major 15 --apps-json "$ROOT/examples/apps-v15.json" --tag "$GOOD_TAG" \
  --frappe-branch version-15-hotfix --python 3.11.9 --node 20.11.1 --context "$TMP/ctx" --push --no-cache --platform linux/arm64 --dry-run
contains "override branch" "$OUT" "FRAPPE_BRANCH=version-15-hotfix"
contains "override python" "$OUT" "PYTHON_VERSION=3.11.9"
contains "override node"   "$OUT" "NODE_VERSION=20.11.1"
contains "push flag"       "$OUT" "--push"
contains "no-cache flag"   "$OUT" "--no-cache"
contains "platform flag"   "$OUT" "--platform linux/arm64"

echo '[]' >"$TMP/empty.json"
expect_ok "empty apps list allowed" --major 15 --apps-json "$TMP/empty.json" --tag "$GOOD_TAG" --context "$TMP/ctx" --dry-run

mkdir -p "$TMP/fd/resources"
expect_ok "existing context: no clone" --major 15 --apps-json "$ROOT/examples/apps-v15.json" --tag "$GOOD_TAG" --context "$TMP/fd" --dry-run
not_contains "no clone line" "$OUT" "git clone"

NEXAPLOY_BUILDER=docker expect_ok "plain docker builder" --major 15 --apps-json "$ROOT/examples/apps-v15.json" --tag "$GOOD_TAG" --context "$TMP/fd" --push --dry-run
contains "docker build"  "$OUT" "docker build --file"
contains "docker push"   "$OUT" "docker push $GOOD_TAG"

expect_ok "registry with port" --major 15 --apps-json "$ROOT/examples/apps-v15.json" --tag "registry.example.com:5000/nexaploy/bench:rg-1" --context "$TMP/fd" --dry-run

cat >"$TMP/private.json" <<'EOF'
[{"url": "https://ghp_SECRET123@github.com/acme/private_app", "branch": "main"}]
EOF
expect_ok "private repo accepted" --major 15 --apps-json "$TMP/private.json" --tag "$GOOD_TAG" --context "$TMP/fd" --dry-run
not_contains "token redacted in dry-run" "$OUT" "$(base64 <"$TMP/private.json" | tr -d '\n')"
contains "redaction marker" "$OUT" "redacted"

# python3 fallback for JSON handling (hide jq from PATH)
mkdir -p "$TMP/nojq"
for t in bash base64 tr date dirname cat grep python3 env sed; do
  p="$(command -v "$t")" && ln -sf "$p" "$TMP/nojq/$t"
done
PATH="$TMP/nojq" expect_ok "python3 fallback ok" --major 16 --apps-json "$ROOT/examples/apps-v16.json" --tag "$GOOD_TAG" --context "$TMP/fd" --dry-run
contains "fallback python v16" "$OUT" "PYTHON_VERSION=3.14.10"
echo '{"url": "x"}' >"$TMP/obj.json"
PATH="$TMP/nojq" expect_fail "python3 fallback rejects object" "must be a JSON array" --major 15 --apps-json "$TMP/obj.json" --tag "$GOOD_TAG" --dry-run
PATH="$TMP/nojq" expect_fail "python3 fallback unknown major" "unknown Frappe major" --major 13 --apps-json "$ROOT/examples/apps-v15.json" --tag "$GOOD_TAG" --dry-run

# ------------------------------------------------------------------ validation errors
A15=("--apps-json" "$ROOT/examples/apps-v15.json")
expect_fail "missing --major"    "--major is required" "${A15[@]}" --tag "$GOOD_TAG" --dry-run
expect_fail "missing --tag"      "--tag is required" --major 15 "${A15[@]}" --dry-run
expect_fail "missing --apps-json" "--apps-json is required" --major 15 --tag "$GOOD_TAG" --dry-run
expect_fail "flag without value" "requires a value" --major --tag "$GOOD_TAG" --dry-run
expect_fail "unknown flag"       "unknown argument" --major 15 "${A15[@]}" --tag "$GOOD_TAG" --bogus --dry-run
expect_fail "unknown major 13"   "unknown Frappe major '13'" --major 13 "${A15[@]}" --tag "$GOOD_TAG" --dry-run
expect_fail "non-numeric major"  "must be a number" --major fifteen "${A15[@]}" --tag "$GOOD_TAG" --dry-run

for bad in "nexaploy/bench" "Nexaploy/Bench:x" "nexaploy/bench:" "nexaploy/bench:-x" "nexaploy/bench:a b" \
           "nexaploy/bench@sha256:abc" "nexaploy//bench:x" "nexaploy/bench:x;rm -rf /"; do
  expect_fail "bad tag '$bad'" "invalid --tag" --major 15 "${A15[@]}" --tag "$bad" --dry-run
done

expect_fail "apps.json missing file" "not found" --major 15 --apps-json "$TMP/nope.json" --tag "$GOOD_TAG" --dry-run
echo '{not json' >"$TMP/broken.json"
expect_fail "apps.json broken"     "not valid JSON" --major 15 --apps-json "$TMP/broken.json" --tag "$GOOD_TAG" --dry-run
expect_fail "apps.json object"     "must be a JSON array" --major 15 --apps-json "$TMP/obj.json" --tag "$GOOD_TAG" --dry-run
echo '["https://github.com/frappe/erpnext"]' >"$TMP/strs.json"
expect_fail "apps.json strings"    "must be an object" --major 15 --apps-json "$TMP/strs.json" --tag "$GOOD_TAG" --dry-run
echo '[{"url": "https://github.com/frappe/erpnext"}]' >"$TMP/nobranch.json"
expect_fail "apps.json no branch"  "branch must be a non-empty string" --major 15 --apps-json "$TMP/nobranch.json" --tag "$GOOD_TAG" --dry-run
echo '[{"branch": "version-15"}]' >"$TMP/nourl.json"
expect_fail "apps.json no url"     "url must be a non-empty string" --major 15 --apps-json "$TMP/nourl.json" --tag "$GOOD_TAG" --dry-run
echo '[{"url": "file:///etc/passwd", "branch": "main"}]' >"$TMP/fileurl.json"
expect_fail "apps.json file url"   "unsupported url" --major 15 --apps-json "$TMP/fileurl.json" --tag "$GOOD_TAG" --dry-run
echo '[{"url": "https://github.com/frappe/erpnext", "branch": "main; rm -rf /"}]' >"$TMP/badbranch.json"
expect_fail "apps.json bad branch" "invalid branch" --major 15 --apps-json "$TMP/badbranch.json" --tag "$GOOD_TAG" --dry-run
echo '[{"url": "https://github.com/frappe/frappe.git", "branch": "version-15"}]' >"$TMP/frappe.json"
expect_fail "apps.json lists frappe" "do not list frappe" --major 15 --apps-json "$TMP/frappe.json" --tag "$GOOD_TAG" --dry-run

expect_fail "bad python"  "invalid --python" --major 15 "${A15[@]}" --tag "$GOOD_TAG" --python 3.x --dry-run
expect_fail "bad node"    "invalid --node"   --major 15 "${A15[@]}" --tag "$GOOD_TAG" --node v18 --dry-run
expect_fail "bad branch"  "invalid --frappe-branch" --major 15 "${A15[@]}" --tag "$GOOD_TAG" --frappe-branch '$(id)' --dry-run
expect_fail "multi-arch without push" "add --push" --major 15 "${A15[@]}" --tag "$GOOD_TAG" --platform linux/amd64,linux/arm64 --dry-run

echo
echo "passed: $PASS  failed: $FAIL"
[[ $FAIL -eq 0 ]]
