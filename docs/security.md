# Security model

## Connections

- **The agent always connects out.** The control plane never connects to a server. The
  agent sends a heartbeat, polls for jobs and reports results over HTTPS. Servers need no
  inbound port except 80/443 for their sites, and the platform needs no SSH access to
  them.
- The local agent API listens on `127.0.0.1:8787` only and requires a bearer token, which
  is stored in `agent.toml` with mode 600.
- **Server tokens.** When a server registers with a single-use registration token (valid
  24 hours by default), it gets a server token. The control plane stores only its SHA-256
  hash and compares tokens in constant time. Archiving a server clears its token.
- Secrets in job parameters, such as an admin password, are resolved only when the agent
  polls for the job. They are not stored in plain text in the job. Parameters whose key
  matches `password|secret|token|key` are hidden in API output and logs, at any depth. They
  are removed from the agent's job store once the job finishes. Registry passwords reach
  `docker login` only on stdin.

## On the server

- Every container runs as a non-root user with a fixed UID: Frappe 3113, MariaDB 3101,
  Redis 3102, Traefik 3100.
- Bench containers are hardened: `no-new-privileges`, tmpfs, resource limits, and per-bench
  internal networks. MariaDB publishes no port. Traefik reads route files only. It does not
  use the Docker socket.
- `agent.toml`, `db.env` (MariaDB root password), `rclone.conf` (S3 keys) and `acme.json`
  have mode 600.
- The agent runs `bench` commands inside the bench containers, never on the host. Commands
  are argument lists, not shell strings. Bench, site, domain and app names are checked
  against strict patterns first.
- **Site SQL** is read-only by default. The **console** is for team admins only, and results
  that may contain sensitive data depend on the team role.

## Control plane

- **Teams.** Every server, site, backup and invoice belongs to a team. Roles are
  Owner, Admin, Developer and Billing. Membership is checked in one place, and members of one
  team cannot see another team's documents, in the dashboard API or in Frappe's own API.
- Teams can require two-factor authentication. API keys are per user, and every customer
  action is written to the team's activity log, with secrets removed.
- **Agent upgrades** are always started by an operator, and only platform admins publish
  Agent Releases. Customers can upgrade their own BYOS servers only if the platform allows
  it. The agent fetches only the git ref (or a `.whl` with an optional SHA-256) named in
  the job. The ref and URLs are validated on both sides.
- Backup downloads use presigned URLs that expire after 15 minutes. The bucket itself stays
  private.
- The dashboard loads no scripts from third-party CDNs: Alpine.js is vendored.

## Reporting a vulnerability

Please report security issues privately to the maintainers (GitHub security advisory on the
repository) rather than in a public issue.
