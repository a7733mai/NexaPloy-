#!/usr/bin/env bash
# DigitalOcean Marketplace image validation: runs DigitalOcean's official checker
# (github.com/digitalocean/marketplace-partners, scripts/99-img-check.sh, Apache-2.0)
# pinned by IMG_CHECK_REF. The build fails when the checker reports a failure.
set -euo pipefail

REF="${IMG_CHECK_REF:-master}"
URL="https://raw.githubusercontent.com/digitalocean/marketplace-partners/${REF}/scripts/99-img-check.sh"
dir="$(mktemp -d)"
trap 'rm -rf "$dir"' EXIT

curl -fsSL --retry 3 -o "$dir/img_check.sh" "$URL"
bash "$dir/img_check.sh" | tee "$dir/out.txt"
if grep -q 'FAIL\]' "$dir/out.txt"; then
    echo "error: the DigitalOcean image check reported failures" >&2
    exit 1
fi
