# Control plane load test

`fake_agents.py` runs N fake agents that speak the real agent protocol (token auth,
`poll` every `--poll` s, realistic `heartbeat` every `--heartbeat` s with facts, health,
OS-update status and 2 benches x 3 sites), with random start offsets like real agents.
`seed.py` creates the matching Server records (billing-exempt team) inside the control plane.

```bash
# 1. create 1000 test servers (prints their tokens)
docker exec -i nexaploy-cp-backend bash -c 'cd /home/frappe/frappe-bench/sites && \
    ../env/bin/python - cp.example.com 1000' < e2e/loadtest/seed.py > tokens.json
# 2. run 1000 agents for 150 s against the all-in-one control plane (127.0.0.1:8090)
python3 e2e/loadtest/fake_agents.py --url http://127.0.0.1:8090 --site-name cp.example.com \
    --tokens tokens.json --servers 1000 --duration 150 --poll 30
# 3. remove the test servers
docker exec -i nexaploy-cp-backend bash -c 'cd /home/frappe/frappe-bench/sites && \
    ../env/bin/python - cp.example.com --cleanup' < e2e/loadtest/seed.py
```

## Results (Frappe v16 all-in-one control plane, 4 vCPU / 15 GB, same host as the agents)

Latency is per request as seen by the agents; CPU in % of one core (docker stats). Above
~80 requests/s a single `fake_agents.py` process is the bottleneck (it showed multi-second
latencies the control plane did not cause), so the larger runs use two processes (`--offset`).

| Servers | Setup | Poll p50 / p95 | Heartbeat p50 / p95 | Errors | Backend CPU | DB CPU |
|---|---|---|---|---|---|---|
| 100 | poll 10 s, 2 workers | 14 / 22 ms | 41 / 58 ms | 0 | 20% | 6% |
| 500 | poll 10 s, 2 workers, empty job table | 14 / 38 ms | 46 / 114 ms | 0 | 59% | 22% |
| 500 | same + 500 000 old Agent Jobs, **no index** | 30 s timeouts | 30 s timeouts | **2499** | 3% | **97%** |
| 500 | same + 500 000 old Agent Jobs, **with indexes** | 14 / 38 ms | 48 / 124 ms | 0 | 56% | 20% |
| 1000 | poll 10 s, 2 workers | 17 / 71 ms | 60 / 150 ms | 0 | ~110% | ~40% |
| 1000 | idle poll 30 s, 2 workers | 16 / 38 ms | 49 / 108 ms | 0 | 60% | 23% |
| 2000 | idle poll 30 s, 2 workers | 25 / 250 ms | 85 / 330 ms | 1 of ~10 000 | ~130% (near the limit) | ~50% |
| 2000 | idle poll 30 s, 9 workers, 4 CPUs | 16 / 60 ms | 52 / 180 ms | 0 | ~130% | ~45% |

Scheduled jobs with 2000 registered servers (`bench execute`, including ~1.2 s start-up):
mark_offline_servers, check_alerts, check_server_health, check_agent_upgrades,
schedule_binlog_archiving, schedule_maintenance, prune_agent_jobs, prune_server_metrics all
finish in 0.9–1.9 s, so they need no batching at this size.

What changed because of these runs:

- **Agent Job indexes** `(server, status)`, `(reference_doctype, reference_name)`, `(status, modified)`:
  without them every poll and heartbeat scanned the whole table and ~10 days of history for
  500 servers took the control plane down. This was the real limit, not the workers.
- **Agent Job retention** (`prune_agent_jobs`, daily): routine jobs 7 days, the rest 90 days.
- **Adaptive agent polling**: 10 s while jobs were seen in the last 5 minutes, otherwise 30 s; a
  heartbeat that reports pending jobs triggers an immediate poll. A third fewer requests.
- **Server Metric roll-up**: 5-minute rows older than 48 h become hourly averages.
- **Workers**: `install.sh --control-plane-workers` (default: 2 x CPUs + 1, max 9, RAM-bounded).

Not measured here: portal users at the same time, real network latency between agents and the
control plane (TLS through Traefik), and a control plane on its own server.
