output "ipv4" {
  description = "Server IPv4: the A record of var.domain."
  value       = hcloud_server.app.ipv4_address
}

output "ipv6" {
  description = "Server IPv6: the AAAA record of var.domain."
  value       = hcloud_server.app.ipv6_address
}

output "volume_device" {
  description = "Block device of the data volume on the server."
  value       = hcloud_volume.data.linux_device
}

output "storage_box_server" {
  description = "Storage Box host for backups (empty without a Storage Box)."
  value       = try(hcloud_storage_box.backups[0].server, "")
}

output "storage_box_username" {
  description = "Storage Box user."
  value       = try(hcloud_storage_box.backups[0].username, "")
}
