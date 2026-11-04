#!/usr/bin/env bash
# Install Nexaploy into the image without a control plane ("register later"):
# Docker, users/UIDs, /opt/nexaploy, the agent venv, frappe_docker and pre-pulled images.
# 90-cleanup.sh then removes everything server-specific (config, secrets, DB data);
# nexaploy-firstboot runs install.sh again on the new server with the user-data settings.
#
# Environment (set by nexaploy.pkr.hcl):
#   NEXAPLOY_REPO, NEXAPLOY_REF     source of the agent (git URL + tag/branch)
#   PREPULL_IMAGES                  images that must be in the image (space separated)
#   OPTIONAL_IMAGES                 images pulled when available (e.g. the control plane image)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

: "${NEXAPLOY_REPO:?}" "${NEXAPLOY_REF:?}"
SRC=/usr/local/share/nexaploy/src

echo "==> fetching $NEXAPLOY_REPO ($NEXAPLOY_REF) into $SRC"
rm -rf "$SRC"
install -d -m 0755 "$(dirname "$SRC")"
git clone --quiet --depth 1 --branch "$NEXAPLOY_REF" "$NEXAPLOY_REPO" "$SRC"
git -C "$SRC" rev-parse HEAD > /usr/local/share/nexaploy/COMMIT
echo "$NEXAPLOY_REF" > /usr/local/share/nexaploy/REF

# Standalone install, no systemd units yet (the agent must not run with this throwaway
# config); install.sh also pulls and starts mariadb + traefik once, which is fine.
echo "==> install.sh (image mode)"
bash "$SRC/agent/install/install.sh" --agent-source "$SRC" --skip-preflight --no-systemd

for image in ${PREPULL_IMAGES:-}; do
    echo "==> docker pull $image"
    docker pull --quiet "$image"
done
for image in ${OPTIONAL_IMAGES:-}; do
    echo "==> docker pull $image (optional)"
    docker pull --quiet "$image" || echo "warning: could not pull $image; it is pulled on first use"
done
docker image ls
