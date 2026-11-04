# Troubleshooting

## Where to look

```bash
nexaploy ping                                   # agent API up? which version?
nexaploy jobs --status failure                  # failed jobs on this server
nexaploy jobs <job id>                          # every step with its output and the error
journalctl -u nexaploy-agent-worker -n 200 -f   # jobs, heartbeat, control plane polling
journalctl -u nexaploy-agent-api -n 200         # local API
docker ps --format '{{.Names}}\t{{.Status}}'    # nexaploy-db, nexaploy-traefik, <bench>-*
docker logs nexaploy-traefik --tail 100         # TLS / routing
docker logs nexaploy-db --tail 100              # MariaDB
cat /var/log/nexaploy-install.log               # servers set up by cloud-init or the server image
```

In the control plane, each **Agent Job** shows the same steps and output. The server page
shows the last heartbeat, the facts (disk, memory, unhealthy containers) and recent jobs.

## Failed jobs

Every job runs in steps, and each step can safely run twice. When a step fails, the job
stops and later steps are skipped. The output of the failed step tells you why.

- **Local jobs** (started with the `nexaploy` CLI or the local API): fix the cause, then
  run `nexaploy retry <job id> --wait`. Steps that already succeeded are skipped, and the
  job continues at the failed step. A job whose secrets were already removed (for example
  an admin password) cannot be retried. Submit it again instead.
- **Control plane jobs** are retried from the control plane: a platform admin calls the
  Agent Job's `retry` method. This creates a new job with the same parameters. A failed site
  move continues when you retry its failed job.
- If the agent restarts in the middle of a job, it re-queues the job on start. Steps that
  already succeeded are skipped.

## TLS certificates (Let's Encrypt)

Traefik gets a certificate for each site with the ACME HTTP challenge. If a site shows a
certificate warning or Traefik's default certificate:

- The site's DNS (A/AAAA record) must point to **this** server. After a move to another
  server, update every domain of the site.
- **Port 80 must be reachable from the internet.** Let's Encrypt validates over port 80, so
  a firewall or security group that allows only 443 breaks it.
- Set a real e-mail with `--acme-email` (run `install.sh` again to change it). The default
  `admin@example.com` is rejected.
- Check `docker logs nexaploy-traefik` for `acme` errors. Let's Encrypt limits how often it
  issues certificates; after a few failures, wait before you try again.
- Certificates are stored in `/opt/nexaploy/proxy/data/certificates/acme.json`, which must
  have mode 600 and be owned by UID 3100.
- Local tests without public DNS cannot get certificates. Use `curl -k --resolve` as in
  `e2e/README.md`.

## Ports 80/443 already in use

`install.sh` stops with `port 80 is already in use` when another web server (nginx, Apache,
Caddy, another Traefik) is listening. Stop it, or use a fresh server. A port held by
Nexaploy's own proxy is fine (re-runs). `--skip-preflight` skips the check, but the proxy will
still fail to start on a port that is in use.

## MariaDB 11.8 and TLS (Frappe v16)

The MariaDB client in Frappe v16 images **requires TLS** to the database. The shared database
is therefore `mariadb:11.8`, which supports TLS out of the box. Symptoms of an older server
(e.g. 10.6) are v16 sites that fail to connect, or `bench new-site` failing with TLS/SSL
errors.

- Run `install.sh` again: the DB stack file uses `mariadb:11.8`. An existing 10.6 data
  directory is upgraded in place, and v15 sites keep working.
- If you overrode the image with `MARIADB_IMAGE` in `/opt/nexaploy/db/.env`, use 11.4 or newer.
- On v16, `bench new-site` uses `--mariadb-user-host-login-scope=%`. The agent falls back to
  `--no-mariadb-socket` only for older versions (v14) that lack the new flag.

## Disk full

- The control plane e-mails the team when a disk is over **85 %**, at most once every 6 hours
  per server.
- Find the space: `df -h /opt/nexaploy`, `du -sh /opt/nexaploy/*`, `docker system df`.
- Common causes are old bench images (`docker image prune` removes images no container
  uses), local backups in `/opt/nexaploy/backups` (lower `retention_days` in `agent.toml`,
  and set up remote storage), and build output in `/opt/nexaploy/images/builds`.
- MariaDB stops when its disk is full. Free some space, then check
  `docker logs nexaploy-db` and `docker ps` (the container is restarted automatically).

## Server shows Offline

A server becomes *Offline* when no heartbeat has arrived for 5 minutes.

- `systemctl status nexaploy-agent-worker`. The worker sends the heartbeat.
- `journalctl -u nexaploy-agent-worker` shows `authentication failed (401)` when the server
  token is wrong. This happens, for example, after the server was archived or its token was
  rotated. Register again with a new registration token and `--force-register`.
- The server needs outbound HTTPS to the control plane. Inbound connections are never needed.

## Agent upgrade failed

See [Agent upgrades](operating.md#agent-upgrades). The job output shows the failed step.
If the new agent does not start, the watchdog rolls back after about 3 minutes. By hand:
`sudo nexaploy-agent rollback`.

## Bench image cannot be pulled on another server

Without a registry, an image exists only on the server that built it. Set **Nexaploy
Settings → Registry** so that builds are pushed and other servers can pull them.
