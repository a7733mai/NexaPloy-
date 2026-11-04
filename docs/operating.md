# Operating

## Backups

- **Storage.** Set the S3-compatible storage in **Nexaploy Settings** (endpoint, region,
  bucket, access key, secret key). Every server receives it through the job
  `server.configure_backups`, which writes `rclone.conf` with mode 600. All servers share one
  bucket under `<prefix>/<server id>/<bench>/<site>/<timestamp>`. This is why a backup
  taken on one server can be restored on another.
- **Schedule.** Each site has a backup frequency: *Daily*, *Every 12 hours*, *Weekly* or
  *Off*. It also has a retention period (14 days by default; 0 keeps backups forever) and
  a choice to include files. A scheduler task creates the due backups every hour. It skips
  a site whose previous backup is still running.
- **Retention.** Once a day, backups older than the retention period are deleted from the
  bucket. The newest 3 are always kept. On the server itself, `agent.toml [backup]
  retention_days` controls the local copies.
- **Manual backup, restore and download** are on the site page. Each file has a SHA-256
  checksum, which is checked before a restore. Download links are presigned and expire
  after 15 minutes.
- On a server without storage settings, backups stay local. The job log shows a warning.

```bash
nexaploy site backup erp.example.com --wait
nexaploy site backups erp.example.com
nexaploy site restore erp.example.com --backup-id <timestamp> --wait
```

## Updates

- **Apps and Frappe** are updated by building a new image for the bench group (**Deploy
  now**). You then update its benches (job `bench.update`): it starts the new image and runs
  `bench migrate` on every site of the bench.
- **Automatic updates.** Turn on *Auto update* on a bench group and set an *update window*
  (`HH:MM-HH:MM` in the server's local time; it may cross midnight). After a successful
  build, benches get `bench.update` inside that window. The control plane checks every
  15 minutes.
- **Infrastructure** (proxy and DB compose files, systemd units): run `install.sh` again on the
  server. It is idempotent and keeps secrets and certificates.

## Agent upgrades

The agent is upgraded from the control plane, never by itself. The operator decides when.

1. A platform admin creates an **Agent Release**: the version (e.g. `0.2.0`), the git ref
   (default `v0.2.0`), the channel (*stable* or *beta*), release notes, and the **Published**
   flag. A server on the *beta* channel is also offered stable releases.
2. A Server shows its agent version and the latest release of its channel. In the desk,
   use **Agent → Upgrade agent to X**. In the dashboard, use **Upgrade agent** next to the
   agent version. On a published release, **Upgrade outdated servers** upgrades every active
   server that runs an older version.
3. Customers can upgrade their own BYOS servers only when **Nexaploy Settings → Allow customers
   to upgrade the agent on their BYOS servers** is on. They must be Owner or Admin of the team.

On the server, the job `agent.upgrade` runs these steps:

| Step | What happens |
|---|---|
| `download` | `git fetch` of the ref into `/opt/nexaploy/agent/src.new`, then re-sync of the bench templates. A release can name a `.whl` URL with a SHA-256 instead. |
| `install` | A new venv at `/opt/nexaploy/agent/.venv.new` |
| `verify` | `.venv.new/bin/nexaploy-agent version` must print the release version, and the agent modules must import |
| `activate` | The running venv becomes `.venv.prev` and the new one becomes `.venv`. The image-builder files are refreshed |
| `restart` | `systemd-run` restarts both units **10 s after** the job reported success. A watchdog checks the agent later |

Benches, sites, MariaDB and the proxy are never touched. The upgrade is **Succeeded** when a
heartbeat reports the new version. It is **Failed** when the job fails, or when no such
heartbeat arrives within *Agent Upgrade Timeout* (15 minutes by default).

**Rollback.**

- *Automatic.* About 3 minutes after the restart, a watchdog checks that the new agent answers
  `/v1/ping` with the new version. It runs from the previous venv. If the check fails, it puts
  `.venv.prev` back and restarts the units.
- *From the control plane.* **Roll back agent** (job `agent.rollback`). The agent must still
  be online to pick up the job.
- *On the server:*

    ```bash
    sudo nexaploy-agent rollback
    # if even the new CLI does not start:
    sudo /opt/nexaploy/agent/.venv.prev/bin/python -m nexaploy_agent.agent_cli rollback
    ```

Standalone servers can queue the same job locally: `sudo nexaploy-agent upgrade --version 0.2.0`.

!!! note
    A server whose agent is older than the first version with `agent.upgrade` cannot run the
    job yet. Run `install.sh` once on it, with `--branch <tag>`.

## Moving sites

A site is never changed in place. To move it, Nexaploy takes a backup and restores it on the
target bench (**Lifecycle → Move** on the site page):

- **Same server, another bench:** backup → drop on the old bench → restore on the target bench.
- **Another server:** backup (uploaded) → restore on the target server (creates the site,
  restores the files and the encryption key, migrates) → drop on the source. This needs the
  backup storage. The dashboard tells you which DNS records must point to the new server.
- Custom domains are added again on the target. If a step fails before the source is
  dropped, the site stays where it was. If a later step fails, the site is marked *Broken*
  and the move backup stays linked to it. Retrying the failed Agent Job continues the move.

Other actions: **staging copy** (a scrubbed clone on the same bench, not billed),
**rename** (the old name is kept as a domain), **transfer** to another team (only on shared
hosted benches).

## Frappe versions

- v14, v15 and v16 run side by side. Each bench group has one Frappe major, and each server
  can run several benches.
- **Default for new bench groups: v16** (Python 3.14, Node 24). v15 uses Python 3.11 and
  Node 18. v14 uses Python 3.10 and Node 16 (`image-builder/versions.json`).
- Marketplace apps declare which majors they support. A bench group only accepts apps that
  have a release for its major.
- All versions share one MariaDB per server: `mariadb:11.8`. The v16 database client requires
  TLS, and MariaDB 11.4+ provides it out of the box.

### Upgrading a site from v15 to v16

1. Create a v16 bench group with the same apps (each app needs a v16 release), deploy it,
   and deploy a bench to the server.
2. Open the site, choose **Move**, select the v16 bench, and confirm the upgrade. The API
   call is `move_site(site, target_bench, allow_upgrade=1)`.
3. Nexaploy takes a backup on v15, restores it on the v16 bench and runs `bench migrate`. The
   data and the custom domains are kept.

A move to a higher major needs `allow_upgrade`. A move to a lower major is refused. Before
you upgrade, check that every custom app works on v16. Staging copies always stay on the
bench of their production site, so they cannot be used to test a move.
