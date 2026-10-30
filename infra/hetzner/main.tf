locals {
  name   = "plujka-${var.environment}"
  labels = { app = "plujka", environment = var.environment }
}

resource "hcloud_ssh_key" "operator" {
  name       = "${local.name}-operator"
  public_key = var.ssh_public_key
  labels     = local.labels
}

# Only HTTP(S) from anywhere; SSH from admin networks. PostgreSQL, OpenSearch, the API and Streamlit
# are not published (deploy/docker-compose.prod.yml); Caddy is the only entry point.
resource "hcloud_firewall" "web" {
  name   = local.name
  labels = local.labels

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = var.admin_cidrs
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "80"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction  = "in"
    protocol   = "udp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction  = "in"
    protocol   = "icmp"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
}

# Data lives on the volume, not on the server's disk: rescaling or replacing the server keeps it.
resource "hcloud_volume" "data" {
  name              = "${local.name}-data"
  size              = var.volume_size_gb
  location          = var.location
  format            = "ext4"
  delete_protection = var.environment == "production"
  labels            = local.labels
}

resource "hcloud_server" "app" {
  name         = local.name
  server_type  = var.server_type
  keep_disk    = var.keep_disk
  image        = var.image
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.operator.id]
  firewall_ids = [hcloud_firewall.web.id]
  labels       = local.labels

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    volume_id      = hcloud_volume.data.id
    ssh_public_key = var.ssh_public_key
    domain         = var.domain
    acme_email     = var.acme_email
    repo_url       = var.repo_url
    git_ref        = var.git_ref
  })

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  # cloud-init runs on first boot only; later changes go through the deploy scripts, not a rebuild.
  lifecycle {
    ignore_changes = [user_data, image, ssh_keys]
  }
}

resource "hcloud_volume_attachment" "data" {
  volume_id = hcloud_volume.data.id
  server_id = hcloud_server.app.id
  automount = false
}

resource "hcloud_storage_box" "backups" {
  count            = var.storage_box_type == "" ? 0 : 1
  name             = "${local.name}-backups"
  storage_box_type = var.storage_box_type
  location         = var.location
  password         = var.storage_box_password
  labels           = local.labels

  access_settings = {
    reachable_externally = true
    ssh_enabled          = true
    samba_enabled        = false
    webdav_enabled       = false
    zfs_enabled          = false
  }

  delete_protection = var.environment == "production"
}
