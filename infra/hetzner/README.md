# Hetzner infrastructure (Terraform)

One server per environment with Docker Compose, a data volume, a firewall and (production) a Storage
Box for backups. The mart is not built on the server: CI builds and checks it and publishes
`mart.dump`; `deploy/deploy_mart.sh` restores it (see [DEPLOYMENT.md](../../docs/DEPLOYMENT.md)).

| Resource | Purpose |
| --- | --- |
| `hcloud_server.app` | Ubuntu 24.04, Docker, `/srv/plujka` (the repository), systemd unit `plujka.service` |
| `hcloud_volume.data` | `/mnt/data`: PostgreSQL, OpenSearch, Caddy certificates, feedback, deploy work files |
| `hcloud_firewall.web` | SSH from `admin_cidrs`, HTTP/HTTPS from anywhere; nothing else is reachable |
| `hcloud_storage_box.backups` | Nightly backups (skipped when `storage_box_type = ""`) |

## First setup

1. Hetzner Cloud project and API token: `export HCLOUD_TOKEN=...`.
2. State bucket in Hetzner Object Storage (console), S3 credentials:
   `export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...`.
3. Copy `backend-production.hcl.example` to `backend-production.hcl` and
   `production.tfvars.example` to `production.tfvars` (both ignored by Git); set the Storage Box
   password with `export TF_VAR_storage_box_password=...`.
4. Create:

   ```bash
   terraform init -backend-config=backend-production.hcl
   terraform apply -var-file=production.tfvars
   ```

5. DNS: point the A and AAAA records of `domain` at the outputs `ipv4` and `ipv6` (any DNS provider).
   Caddy requests the certificate once the records resolve.
6. Secrets: copy `deploy/env.production.example` to the server and fill it in; nothing secret is in
   Terraform state or Git:

   ```bash
   scp deploy/env.production.example deploy@<ipv4>:/srv/plujka/.env
   ssh deploy@<ipv4> 'chmod 600 /srv/plujka/.env && nano /srv/plujka/.env && sudo systemctl start plujka'
   ```

7. Mart: `ssh deploy@<ipv4> /srv/plujka/deploy/deploy_mart.sh` (about 6 minutes).

Staging uses the same module with its own state key and `staging.tfvars` (smaller server, no
Storage Box).

## Resize and replacement

- **Rescale:** change `server_type` and `terraform apply`. Hetzner stops the server, changes the type
  and boots it (a few minutes of downtime). With `keep_disk = true` (default) the system disk keeps its
  size, so the server can be scaled down again later.
- **More data:** raise `volume_size_gb` and apply, then `sudo resize2fs /dev/disk/by-id/scsi-0HC_Volume_*`
  on the server. Volumes only grow.
- **Replace the server** (new image, broken system): `terraform apply -replace=hcloud_server.app`. The
  volume is detached and attached to the new server with its data; copy `.env` again and start the
  service. The production volume and Storage Box have delete protection.

cloud-init runs only on the first boot of a server; later changes to `cloud-init.yaml.tftpl` apply to
new servers only (`ignore_changes`).
