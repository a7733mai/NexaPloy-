variable "nexaploy_repo" {
  type        = string
  default     = "https://github.com/a7733mai/NexaPloy-.git"
  description = "Git repository with the agent and install.sh."
}

variable "nexaploy_ref" {
  type        = string
  default     = "main"
  description = "Tag or branch to bake into the image (use a release tag, e.g. v0.1.0)."
}

variable "prepull_images" {
  type        = list(string)
  description = "Images every server needs; the build fails when one cannot be pulled."
  default = [
    "mariadb:11.8",
    "traefik:v3.3",
    "redis:7-alpine",
    "frappe/erpnext:v16",
  ]
}

variable "optional_images" {
  type        = list(string)
  description = "Pulled when available (the all-in-one control plane image)."
  default     = ["ghcr.io/a7733mai/nexaploy-control-plane:16"]
}

variable "zero_free_space" {
  type        = bool
  default     = false
  description = "Write zeros over free disk space before the snapshot (smaller DigitalOcean snapshots, slower build)."
}

# --- Hetzner Cloud ---------------------------------------------------------------------------
variable "hcloud_token" {
  type      = string
  default   = env("HCLOUD_TOKEN")
  sensitive = true
}

variable "hcloud_location" {
  type    = string
  default = "fsn1"
}

variable "hcloud_server_type" {
  type        = string
  default     = "cx22"
  description = "Build server type; the snapshot works on this type and larger ones."
}

# --- DigitalOcean ----------------------------------------------------------------------------
variable "do_token" {
  type      = string
  default   = env("DIGITALOCEAN_TOKEN")
  sensitive = true
}

variable "do_region" {
  type    = string
  default = "fra1"
}

variable "do_size" {
  type        = string
  default     = "s-2vcpu-4gb"
  description = "Build droplet size; Marketplace images can then be used on this size and larger."
}

variable "do_snapshot_regions" {
  type    = list(string)
  default = []
}

variable "do_img_check_ref" {
  type        = string
  default     = "master"
  description = "Ref of github.com/digitalocean/marketplace-partners whose 99-img-check.sh validates the image."
}
