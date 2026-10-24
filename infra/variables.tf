variable "project" {
  description = "Dedicated project; short enough for its Linux bridge names."
  type        = string
  default     = "lh-incus"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,8}$", var.project)) && var.project != "default"
    error_message = "Use 2-9 lowercase letters, digits or hyphens; not default."
  }
}
variable "remote" {
  description = "Existing Incus CLI remote."
  type        = string
  default     = "local"
}
variable "storage_pool" {
  description = "Existing Incus storage pool; never created or destroyed here."
  type        = string
  default     = "default"
}
variable "image" {
  description = "Cloud-init-enabled VM image with an Incus agent."
  type        = string
  default     = "images:ubuntu/24.04/cloud"
}
variable "worker_count" {
  type    = number
  default = 3
  validation {
    condition     = var.worker_count >= 1 && var.worker_count <= 8 && floor(var.worker_count) == var.worker_count
    error_message = "worker_count must be an integer between 1 and 8."
  }
}
variable "server_cpu" {
  type    = number
  default = 2
}
variable "server_memory" {
  type    = string
  default = "4GiB"
}
variable "worker_cpu" {
  type    = number
  default = 4
}
variable "worker_memory" {
  type    = string
  default = "6GiB"
}
variable "root_size" {
  type    = string
  default = "30GiB"
}
variable "disk_sizes" {
  description = "Virtual capacities, not guaranteed free host space."
  type        = object({ k3s = string, logs = string, v1 = string, v2 = string })
  default     = { k3s = "30GiB", logs = "10GiB", v1 = "40GiB", v2 = "40GiB" }
}
variable "network_stack" {
  type    = string
  default = "dual"
  validation {
    condition     = contains(["ipv4", "ipv6", "dual", "dual6"], var.network_stack)
    error_message = "Choose ipv4, ipv6, dual, or dual6."
  }
}
variable "management_cidr" {
  type    = string
  default = "172.28.155.0/24"
  validation {
    condition     = can(cidrnetmask(var.management_cidr)) && endswith(var.management_cidr, "/24")
    error_message = "Use an IPv4 /24 management network."
  }
}
variable "cluster_ipv4_cidr" {
  type    = string
  default = "172.28.156.0/24"
  validation {
    condition     = can(cidrnetmask(var.cluster_ipv4_cidr)) && endswith(var.cluster_ipv4_cidr, "/24")
    error_message = "Use an IPv4 /24 node network."
  }
}
variable "cluster_ipv6_cidr" {
  type    = string
  default = "fd00:1c05:156::/64"
  validation {
    condition     = can(cidrhost(var.cluster_ipv6_cidr, 1)) && strcontains(var.cluster_ipv6_cidr, ":") && endswith(var.cluster_ipv6_cidr, "/64")
    error_message = "Use an IPv6 /64 node network."
  }
}
variable "storage_ipv4_cidr" {
  type    = string
  default = "10.76.0.0/16"
  validation {
    condition     = can(cidrnetmask(var.storage_ipv4_cidr)) && endswith(var.storage_ipv4_cidr, "/16")
    error_message = "Use an IPv4 /16 for per-node /24 storage pod subnets."
  }
}
variable "storage_ipv6_cidr" {
  type    = string
  default = "fd00:1c05:168::/48"
  validation {
    condition     = can(cidrhost(var.storage_ipv6_cidr, 1)) && strcontains(var.storage_ipv6_cidr, ":") && endswith(var.storage_ipv6_cidr, "/48")
    error_message = "Use an IPv6 /48 for per-node /64 storage pod subnets."
  }
}
variable "enable_storage_network" {
  type    = bool
  default = false
}
variable "enable_v2" {
  type    = bool
  default = true
}
variable "shared_directory" {
  description = "Optional absolute directory on the Incus HOST, not the client."
  type        = string
  default     = ""
  validation {
    condition     = var.shared_directory == "" || startswith(var.shared_directory, "/")
    error_message = "Use an absolute path on the Incus host, or an empty string."
  }
}

variable "bootstrap_agent" {
  description = "Bootstrap the Incus agent via config disks for stock Ubuntu cloud images."
  type        = bool
  default     = false
}
