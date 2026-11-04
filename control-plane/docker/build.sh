#!/usr/bin/env bash
# Build the Nexaploy control plane image.
#
#   control-plane/docker/build.sh --base <contract image> --tag <tag>
#   control-plane/docker/build.sh --from-official frappe/erpnext:v16.53.0 --tag <tag>
#
# --from-official first adapts an official frappe/erpnext image to the Nexaploy image
# contract (e2e/build-test-image.sh, no network needed), then adds the app.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$HERE")"
REPO="$(dirname "$APP_DIR")"
BASE=""; OFFICIAL=""; TAG=""; PUSH=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base) BASE="$2"; shift 2 ;;
        --from-official) OFFICIAL="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        --push) PUSH=1; shift ;;
        -h|--help) sed -n 2,9p "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$TAG" ]] || { echo "--tag is required" >&2; exit 2; }
[[ -n "$BASE" || -n "$OFFICIAL" ]] || { echo "--base or --from-official is required" >&2; exit 2; }
if [[ -n "$OFFICIAL" ]]; then
    BASE="${TAG%:*}:base-$(echo "$OFFICIAL" | tr -c 'a-zA-Z0-9.\n' '-')"
    "$REPO/e2e/build-test-image.sh" "$OFFICIAL" "$BASE" >/dev/null
fi
docker build --build-arg BASE="$BASE" -f "$HERE/Containerfile" -t "$TAG" "$APP_DIR"
if [[ -n "$OFFICIAL" ]]; then docker rmi "$BASE" >/dev/null 2>&1 || true; fi
[[ $PUSH -eq 1 ]] && docker push "$TAG"
echo "$TAG"
