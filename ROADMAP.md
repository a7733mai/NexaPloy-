# Roadmap

## Phase 0 — Manual proof (1 week)
Run one bench via `bench-template/` by hand on a VPS, create a site, attach a domain with TLS,
back up to S3, restore. If this does not work by hand, nothing above it will.

## Phase 1 — Agent (3–4 weeks)
`agent/`: job queue, all job types in ARCHITECTURE.md §7, `install.sh`, CLI.

## Phase 2 — Control plane (3–4 weeks)
`control-plane/nexaploy_press`: Servers, Release Groups, Benches, Sites, Backups, Agent Jobs.

## Phase 3 — BYOS + billing (2 weeks)
Registration tokens, "Add server" flow, plans & subscriptions, payment gateway webhooks.

## Phase 4 — Marketplace + beta
Marketplace apps with per-major releases, paid apps, 3–5 real beta customers.

## Later
Monitoring (Prometheus/Grafana), auto updates, site moves between servers, customer portal UI.
