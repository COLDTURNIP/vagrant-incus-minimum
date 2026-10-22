terraform {
  required_version = ">= 1.9.0"
  required_providers {
    incus = {
      source  = "lxc/incus"
      version = "1.2.1"
    }
  }
}

provider "incus" {}
