#!/usr/bin/env bash
# Build a Nexaploy-contract bench image from the official frappe/erpnext image, with no
# network access needed during the build (CI / sandboxes where deb.debian.org is blocked).
#
#   e2e/build-test-image.sh [BASE_IMAGE] [TAG]
#
# The official image declares sites/assets as a VOLUME, which a derived image cannot
# undo, so the result is flattened (export/import) to drop it.
set -euo pipefail
BASE="${1:-mirror.gcr.io/frappe/erpnext:v15.89.0}"
TAG="${2:-nexaploy/bench:e2e-v15-20261104T0000}"
HERE="$(cd "$(dirname "$0")" && pwd)"

docker build -q --build-arg BASE="$BASE" -f "$HERE/Containerfile.official" -t "$TAG-layered" "$HERE" >/dev/null
mapfile -t ENVS < <(docker inspect "$TAG-layered" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed '/^$/d')
changes=(--change 'USER frappe' --change 'WORKDIR /home/frappe/frappe-bench'
         --change 'ENTRYPOINT ["/usr/local/bin/nexaploy-entrypoint.sh"]'
         --change 'CMD ["/home/frappe/frappe-bench/env/bin/gunicorn","--chdir=/home/frappe/frappe-bench/sites","--bind=0.0.0.0:8000","--threads=4","--workers=2","--worker-class=gthread","--worker-tmp-dir=/dev/shm","--timeout=120","--preload","frappe.app:application"]'
         --change 'VOLUME ["/home/frappe/frappe-bench/sites","/home/frappe/frappe-bench/logs"]')
for e in "${ENVS[@]}"; do changes+=(--change "ENV $e"); done
cid="$(docker create "$TAG-layered")"
trap 'docker rm -f "$cid" >/dev/null' EXIT
docker export "$cid" | docker import "${changes[@]}" - "$TAG" >/dev/null
docker rm -f "$cid" >/dev/null; trap - EXIT
docker rmi "$TAG-layered" >/dev/null
echo "$TAG"
