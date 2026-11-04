# FAQ

**Is Nexaploy a fork of Frappe Cloud (Press)?**
No. It uses the same ideas (release groups, benches, sites, an agent per server) but it is a
new, smaller code base. It runs on plain Docker Compose and the official `frappe_docker` build.

**What happens to my sites if I stop using Nexaploy?**
They keep running. Removing the agent (`systemctl disable --now nexaploy-agent-api
nexaploy-agent-worker`) leaves the benches, sites, MariaDB and the proxy running. They are
ordinary Docker Compose projects in `/opt/nexaploy`.

**Which Frappe / ERPNext versions are supported?**
v14, v15 and v16, side by side on one server. New bench groups default to v16. See
[Frappe versions](operating.md#frappe-versions).

**Which operating systems?**
Ubuntu 22.04 and 24.04 (x86_64 or arm64). Debian 12 works on a best-effort basis.

**Does the platform need SSH or open ports on my server?**
No. The agent connects out to the control plane. Only 80/443 are open, for your sites.

**Can I use my own domain?**
Yes. Point the domain's DNS to the server and add the domain to the site. Traefik gets a
Let's Encrypt certificate for it.

**Where are backups stored?**
In the S3-compatible bucket configured by the platform (one shared bucket, one folder per
server). Without one, they stay on the server. Google Drive and OneDrive support is being
added.

**Can one server run sites from several teams?**
A team's own (BYOS) server runs that team's benches. Shared hosted servers run sites of
many teams, each with its own database and files.

**How do I upgrade a site from v15 to v16?**
Move it to a v16 bench with the upgrade option. See
[Upgrading a site from v15 to v16](operating.md#upgrading-a-site-from-v15-to-v16).

**How is the agent updated?**
An operator starts the upgrade from the control plane, from published Agent Releases. The
agent keeps the previous version and rolls back by itself if the new one does not start.
See [Agent upgrades](operating.md#agent-upgrades).

**Can I run everything on one server?**
Yes. The all-in-one install (`install.sh --with-control-plane <domain>`) is being added now.
See [Quick start](quick-start.md#all-in-one).

**Is the dashboard available in Arabic?**
Yes. The customer dashboard and this documentation are in English and Arabic, with full
right-to-left layout.
