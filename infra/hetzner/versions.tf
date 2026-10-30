terraform {
  required_version = ">= 1.6"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.70"
    }
  }

  # State in Hetzner Object Storage (S3 API); bucket, key and endpoint come from
  # `terraform init -backend-config=backend-<env>.hcl` (see README.md), credentials from
  # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY.
  backend "s3" {
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
    use_path_style              = true
    use_lockfile                = true
  }
}

# Token from HCLOUD_TOKEN.
provider "hcloud" {}
