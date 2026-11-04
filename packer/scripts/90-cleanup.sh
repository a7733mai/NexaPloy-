#!/usr/bin/env bash
# Make the snapshot generic: no Nexaploy secrets or state, no SSH keys, no logs or history.
# Follows DigitalOcean's marketplace-partners scripts/90-cleanup.sh (Apache-2.0) plus the
# Nexaploy-specific part. Every server made from the image creates its own DB root password,
# agent token and server id on first boot (nexaploy-firstboot -> install.sh).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
ROOT=/opt/nexaploy

# ---- Nexaploy: stop the stacks started by install.sh and wipe per-server state ----------------
for stack in proxy db; do
    if [[ -f "$ROOT/$stack/docker-compose.yml" ]]; then
        docker compose -p "nexaploy-$stack" --project-directory "$ROOT/$stack" down --remove-orphans || true
    fi
done
rm -f "$ROOT/config/agent.toml" "$ROOT/config/db.env" "$ROOT/config/rclone.conf"
find "$ROOT/db/data" "$ROOT/agent/data" "$ROOT/proxy/logs" "$ROOT/backups" -mindepth 1 -delete 2>/dev/null || true
if [[ -f "$ROOT/proxy/data/certificates/acme.json" ]]; then
    : > "$ROOT/proxy/data/certificates/acme.json"
fi
rm -f /var/lib/nexaploy/firstboot.done /etc/nexaploy/firstboot.env
# a fresh Docker engine id per server
systemctl stop docker docker.socket >/dev/null 2>&1 || true
rm -f /var/lib/docker/engine-id

# DigitalOcean build droplets come with their agent; the marketplace check refuses it
if [[ -d /opt/digitalocean ]]; then
    apt-get -y -qq purge droplet-agent do-agent >/dev/null 2>&1 || true
    rm -rf /opt/digitalocean
fi

# ---- generic image cleanup -----------------------------------------------------------------
[[ -d /tmp ]] || mkdir /tmp
chmod 1777 /tmp
apt-get -y -qq update
apt-get -y -qq -o Dpkg::Options::="--force-confold" upgrade
apt-get -y -qq autoremove
apt-get -y -qq autoclean

rm -rf /tmp/* /var/tmp/*
history -c || true
cat /dev/null > /root/.bash_history
unset HISTFILE
find /var/log -type f -exec truncate -s 0 {} \;
rm -rf /var/log/*.gz /var/log/*.[0-9] /var/log/*-????????
cloud-init clean --logs >/dev/null 2>&1 || true
rm -rf /var/lib/cloud/instances/*
rm -f /root/.ssh/authorized_keys /etc/ssh/*key*
touch /etc/ssh/revoked_keys
chmod 600 /etc/ssh/revoked_keys
# a fresh machine id per server (systemd creates it on the next boot)
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id

if [[ "${ZERO_FREE_SPACE:-0}" == "1" ]]; then
    echo "==> zeroing free disk space (smaller snapshot; takes a few minutes)"
    dd if=/dev/zero of=/zerofile bs=4M status=none || true
    rm -f /zerofile
fi
sync
