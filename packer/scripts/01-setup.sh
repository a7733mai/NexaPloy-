#!/usr/bin/env bash
# Base OS for the Nexaploy image: security updates, firewall, permissions of the uploaded files.
# (DigitalOcean Marketplace layout: 01-setup -> 90-cleanup -> 99-img-check)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# cloud-init of the build VM may still be running apt
cloud-init status --wait >/dev/null 2>&1 || true

apt-get -qq update
apt-get -y -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" upgrade
apt-get -y -qq install ca-certificates curl git jq ufw unattended-upgrades >/dev/null

# Firewall: SSH + HTTP(S) only (Traefik serves 80/443; the agent API stays on 127.0.0.1).
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

# Files uploaded by the file provisioner (packer/files/) may lose their mode bits.
chmod 0755 /usr/local/sbin/nexaploy-firstboot /var/lib/cloud/scripts/per-instance/001_onboot /etc/update-motd.d/99-nexaploy
install -d -m 0755 /etc/nexaploy /var/lib/nexaploy
