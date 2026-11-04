# Work in progress

Merged: the AGENT side of server maintenance (OS updates, health, cleanup jobs) and of easy
control-plane deployment (zero-flag image resolution/local build, DNS preflight,
`--restore-control-plane`, `controlplane.update` job, `nexaploy controlplane` CLI).

Still to do (control plane side):
- JOB_KINDS + Agent Job options for server.os_update / os_reboot / os_configure / diagnose /
  cleanup and controlplane.update; callbacks.
- Server fields, heartbeat ingest of facts.os_updates / facts.health, evaluate_health,
  notifications server_health_critical / server_health_ok, maintenance window + weekly
  cleanup schedulers, auto cleanup on critical disk.
- Server page cards (System updates, Health, Clean up), admin fleet health + bulk actions,
  "Update control plane" in /dashboard/admin, first-run setup checklist.
- all_in_one.mark_host: clear the old host flag after a restore.
- README "Deploy the control plane in 5 minutes", ARCHITECTURE §5.9, CHANGELOG,
  release.yml building from the pinned official image.
