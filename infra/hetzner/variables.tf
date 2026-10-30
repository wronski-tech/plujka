variable "environment" {
  description = "Environment name, used in resource names and labels (staging, production)."
  type        = string
}

variable "location" {
  description = "Hetzner location of the server, volume and Storage Box."
  type        = string
  default     = "fsn1"
}

variable "server_type" {
  description = "Server type. Changing it rescales the server in place (reboot); see keep_disk."
  type        = string
  default     = "cx43"
}

variable "keep_disk" {
  description = "Keep the system disk size when rescaling up, so the server can be scaled down again later."
  type        = bool
  default     = true
}

variable "image" {
  description = "System image."
  type        = string
  default     = "ubuntu-24.04"
}

variable "ssh_public_key" {
  description = "Public key of the operator (login as user deploy)."
  type        = string
}

variable "admin_cidrs" {
  description = "Networks allowed to reach SSH."
  type        = list(string)
}

variable "volume_size_gb" {
  description = "Size of the data volume (PostgreSQL, OpenSearch, feedback, backups staging). Can only grow."
  type        = number
  default     = 100
}

variable "domain" {
  description = "Public host name served by Caddy (A/AAAA records point to the server)."
  type        = string
}

variable "acme_email" {
  description = "E-mail for Let's Encrypt."
  type        = string
}

variable "repo_url" {
  description = "Git repository cloned on the server."
  type        = string
  default     = "https://github.com/wronski-tech/plujka.git"
}

variable "git_ref" {
  description = "Branch or tag checked out on first boot."
  type        = string
  default     = "main"
}

variable "storage_box_type" {
  description = "Storage Box type for backups; empty string creates none (e.g. staging)."
  type        = string
  default     = "bx11"
}

variable "storage_box_password" {
  description = "Storage Box password (Hetzner password policy). Only used when a Storage Box is created."
  type        = string
  sensitive   = true
  default     = null
}
