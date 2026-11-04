# Nexaploy server image: Ubuntu 24.04 + Docker + the Nexaploy agent ("register later"),
# with the Frappe/infra images pre-pulled. Builds a Hetzner Cloud snapshot and/or a
# DigitalOcean snapshot (Marketplace layout). See README.md.
#
#   packer init packer/
#   packer build -only='hcloud.nexaploy' -var nexaploy_ref=v0.1.0 packer/
#   packer build -only='digitalocean.nexaploy' -var nexaploy_ref=v0.1.0 packer/

packer {
  required_version = ">= 1.10.0"
  required_plugins {
    hcloud = {
      source  = "github.com/hetznercloud/hcloud"
      version = ">= 1.6.0"
    }
    digitalocean = {
      source  = "github.com/digitalocean/digitalocean"
      version = ">= 1.4.0"
    }
  }
}

locals {
  timestamp = formatdate("YYYYMMDD-hhmm", timestamp())
  version   = trimprefix(var.nexaploy_ref, "v")
  name      = "nexaploy-${replace(local.version, "/[^a-z0-9-]/", "-")}-ubuntu-24-04-${local.timestamp}"
}

source "hcloud" "nexaploy" {
  token         = var.hcloud_token
  image         = "ubuntu-24.04"
  location      = var.hcloud_location
  server_type   = var.hcloud_server_type
  ssh_username  = "root"
  snapshot_name = local.name
  snapshot_labels = {
    nexaploy = "true"
    ref      = replace(var.nexaploy_ref, "/[^A-Za-z0-9._-]/", "-")
    os       = "ubuntu-24.04"
  }
}

source "digitalocean" "nexaploy" {
  api_token        = var.do_token
  image            = "ubuntu-24-04-x64"
  region           = var.do_region
  size             = var.do_size
  ssh_username     = "root"
  snapshot_name    = local.name
  snapshot_regions = var.do_snapshot_regions
  tags             = ["nexaploy-image-build"]
}

build {
  sources = ["source.hcloud.nexaploy", "source.digitalocean.nexaploy"]

  # first-boot script (cloud-init per-instance), motd hint
  provisioner "file" {
    source      = "${path.root}/files/"
    destination = "/"
  }

  provisioner "shell" {
    environment_vars = [
      "NEXAPLOY_REPO=${var.nexaploy_repo}",
      "NEXAPLOY_REF=${var.nexaploy_ref}",
      "PREPULL_IMAGES=${join(" ", var.prepull_images)}",
      "OPTIONAL_IMAGES=${join(" ", var.optional_images)}",
      "ZERO_FREE_SPACE=${var.zero_free_space ? "1" : "0"}",
    ]
    scripts = [
      "${path.root}/scripts/01-setup.sh",
      "${path.root}/scripts/02-nexaploy.sh",
      "${path.root}/scripts/90-cleanup.sh",
    ]
  }

  # DigitalOcean Marketplace requirement: their image validation must pass
  provisioner "shell" {
    only             = ["digitalocean.nexaploy"]
    environment_vars = ["IMG_CHECK_REF=${var.do_img_check_ref}"]
    scripts          = ["${path.root}/scripts/99-img-check.sh"]
  }

  post-processor "manifest" {
    output     = "${path.root}/manifest.json"
    strip_path = true
    custom_data = {
      nexaploy_ref = var.nexaploy_ref
    }
  }
}
