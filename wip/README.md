# Work in progress (snapshots)

Snapshots of features that were still being built when this was saved, as patches against
commit `c7f5033`. Apply one on a branch to continue:

```bash
git checkout -b continue-maintenance c7f5033
git apply --3way wip/server-maintenance.patch
```

- `server-maintenance.patch`: OS updates from the dashboard, server health, cleanup through the agent.
- `control-plane-deploy.patch`: zero-flag all-in-one image resolution/build, DNS preflight,
  control plane self-update, restore onto a new server, first-run checklist.

When the finished versions are merged into this branch, these files are deleted.
