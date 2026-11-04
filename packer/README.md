# Nexaploy server images (Packer)

Ubuntu 24.04 snapshots for **Hetzner Cloud** and **DigitalOcean** that boot straight into a
Nexaploy server. The image contains:

- Docker CE, rclone, ufw (22/80/443 open), security updates applied
- the Nexaploy agent installed by `install.sh` in its venv (`/opt/nexaploy/agent/.venv`),
  the `/opt/nexaploy` tree, the fixed UIDs, `frappe_docker` for image builds
- pre-pulled images: `mariadb:11.8`, `traefik:v3.3`, `redis:7-alpine`, `frappe/erpnext:v16`,
  and the control plane image `ghcr.io/a7733mai/nexaploy-control-plane:16` when it exists
- **no** server-specific state: no `agent.toml`, no DB root password, no MariaDB data, no
  certificates, no SSH keys or machine id (`scripts/90-cleanup.sh`). Each server creates
  its own on first boot.

The agent is installed but not registered ("register later"). On first boot,
`nexaploy-firstboot` (run by cloud-init from
`/var/lib/cloud/scripts/per-instance/001_onboot`) reads the user-data and runs `install.sh`
from the baked source (`/usr/local/share/nexaploy/src`, no OS packages to download):

| User-data | First boot runs |
|---|---|
| `NEXAPLOY_CONTROL_PLANE=https://cp.example.com` + `NEXAPLOY_TOKEN=<registration token>` | `install.sh --control-plane … --token …` (server joins that control plane) |
| `NEXAPLOY_DOMAIN=cloud.example.com` | `install.sh --with-control-plane cloud.example.com` (all-in-one) |
| `NEXAPLOY_ACME_EMAIL=ops@example.com` (optional) | adds `--acme-email` |
| nothing | nothing; the login banner says how to finish |

User-data can be plain `KEY=VALUE` lines:

```
NEXAPLOY_CONTROL_PLANE=https://cp.example.com
NEXAPLOY_TOKEN=Yx2…registration-token…
NEXAPLOY_ACME_EMAIL=ops@example.com
```

…or `# NEXAPLOY_…=` comment lines inside a `#cloud-config`, or `export NEXAPLOY_…=` lines in a
shell script. Only these keys are read; values are validated and never evaluated. Output goes
to `/var/log/nexaploy-install.log`. To finish by hand (no user-data given):

```bash
sudo nexaploy-firstboot --control-plane https://cp.example.com --token <token>
sudo nexaploy-firstboot --domain cloud.example.com --acme-email ops@example.com
```

Hosted servers created by the control plane (Cloud Provider → *Default Image* = this snapshot)
keep working: their `#cloud-config` runs `install.sh` with a registration token as before. On
this image the install is just faster because nothing has to be downloaded.

## Build

```bash
packer init packer/
export HCLOUD_TOKEN=...            # Hetzner Cloud project token (read & write)
export DIGITALOCEAN_TOKEN=...      # DigitalOcean personal access token
packer build -var nexaploy_ref=v0.1.0 packer/                          # both clouds
packer build -only='hcloud.nexaploy' -var nexaploy_ref=v0.1.0 packer/  # Hetzner only
```

Variables (`variables.pkr.hcl`): `nexaploy_repo`, `nexaploy_ref` (use a release tag),
`prepull_images`, `optional_images`, `hcloud_location` / `hcloud_server_type`,
`do_region` / `do_size` / `do_snapshot_regions`, `zero_free_space`, `do_img_check_ref`.
Build results are written to `packer/manifest.json`.

## Layout

```
packer/
├── nexaploy.pkr.hcl          # sources hcloud + digitalocean, provisioners
├── variables.pkr.hcl
├── files/                    # copied to / on the image
│   ├── usr/local/sbin/nexaploy-firstboot
│   ├── var/lib/cloud/scripts/per-instance/001_onboot
│   └── etc/update-motd.d/99-nexaploy
├── scripts/
│   ├── 01-setup.sh           # updates, ufw, permissions
│   ├── 02-nexaploy.sh        # clone the repo at nexaploy_ref, install.sh (no systemd), pre-pull
│   ├── 90-cleanup.sh         # wipe Nexaploy state + DigitalOcean's generic cleanup
│   └── 99-img-check.sh       # DigitalOcean's marketplace image check (DO builds only)
└── tests/test_firstboot.sh   # user-data parsing (dry run): bash packer/tests/test_firstboot.sh
```

The layout follows DigitalOcean's
[marketplace-partners](https://github.com/digitalocean/marketplace-partners) conventions
(`files/`, `scripts/01-setup`, `90-cleanup`, `99-img-check`, per-instance first-boot script).
For a Marketplace submission, use the DigitalOcean snapshot built here, then submit it in the
vendor portal with the user-data keys above as the "getting started" instructions.

## Notes

- The image does not include bench images for your Release Groups (`image-builder/` builds one
  per group). `frappe/erpnext:v16` is the base of the all-in-one control plane image and of the
  e2e test images (`e2e/build-test-image.sh`).
- Registration tokens are single-use. The token stays in the instance metadata but is useless
  once the server has registered.
- `--with-control-plane` (all-in-one) needs an `install.sh` that supports it.
