resource "incus_project" "cluster" {
  name          = var.project
  remote        = var.remote
  force_destroy = false
  description   = "Disposable Longhorn test VMs"
  config = {
    "features.images"          = "false"
    "features.profiles"        = "true"
    "features.networks"        = "false"
    "features.storage.volumes" = "true"
  }
}

# Managed Linux bridges live in the default project. No default profile or
# pre-existing network/pool is modified; only these explicitly named bridges.
resource "incus_network" "management" {
  name    = "${var.project}-mgmt"
  project = "default"
  remote  = var.remote
  type    = "bridge"
  config = {
    "ipv4.address"     = "${cidrhost(var.management_cidr, 1)}/24"
    "ipv4.nat"         = "true"
    "ipv4.dhcp.ranges" = "${cidrhost(var.management_cidr, 100)}-${cidrhost(var.management_cidr, 200)}"
    "ipv6.address"     = "none"
  }
}
resource "incus_network" "cluster" {
  name    = "${var.project}-node"
  project = "default"
  remote  = var.remote
  type    = "bridge"
  config = {
    "ipv4.address" = local.ipv4 ? "${cidrhost(var.cluster_ipv4_cidr, 1)}/24" : "none"
    "ipv6.address" = local.ipv6 ? "${cidrhost(var.cluster_ipv6_cidr, 1)}/64" : "none"
    "ipv4.nat"     = "false"
    "ipv6.nat"     = "false"
    "ipv4.dhcp"    = "false"
    "ipv6.dhcp"    = "false"
    "ipv6.ra"      = "false"
  }
}
resource "incus_profile" "node" {
  name    = "node"
  project = incus_project.cluster.name
  remote  = var.remote
  device {
    name       = "root"
    type       = "disk"
    properties = { path = "/", pool = var.storage_pool, size = var.root_size, "io.cache" = "none" }
  }
  dynamic "device" {
    for_each = var.shared_directory == "" ? [] : [var.shared_directory]
    content {
      name       = "shared"
      type       = "disk"
      properties = { source = device.value, path = "/incus_shared", "io.bus" = "virtiofs" }
    }
  }
}
resource "incus_storage_volume" "data" {
  for_each     = local.disks
  name         = "${local.nodes[each.value.node].name}-${each.value.kind}"
  project      = incus_project.cluster.name
  remote       = var.remote
  pool         = var.storage_pool
  type         = "custom"
  content_type = "block"
  config       = { size = each.value.size }
}
resource "terraform_data" "layout" {
  input = {
    network_stack = var.network_stack
    management    = var.management_cidr
    cluster_v4    = var.cluster_ipv4_cidr
    cluster_v6    = var.cluster_ipv6_cidr
    storage       = var.enable_storage_network
    storage_v4    = var.storage_ipv4_cidr
    storage_v6    = var.storage_ipv6_cidr
  }
}
resource "incus_instance" "node" {
  for_each = local.nodes
  name     = each.value.name
  project  = incus_project.cluster.name
  remote   = var.remote
  image    = var.image
  type     = "virtual-machine"
  profiles = [incus_profile.node.name]
  config = {
    "limits.cpu"     = tostring(each.value.cpu)
    "limits.memory"  = each.value.memory
    "boot.autostart" = "false"
    "cloud-init.user-data" = join("\n", ["#cloud-config", yamlencode({
      hostname         = each.value.name
      manage_etc_hosts = true
      package_update   = true
      packages         = ["python3", "python3-apt"]
      bootcmd = var.bootstrap_agent ? [
        ["mkdir", "-p", "/mnt/incus-agent"],
        ["mount", "/dev/disk/by-label/incus-agent", "/mnt/incus-agent"],
        ["sh", "-c", "cd /mnt/incus-agent && ./install.sh"],
        ["umount", "/mnt/incus-agent"],
      ] : []
    })])
    "cloud-init.network-config" = yamlencode({
      version = 2
      ethernets = {
        management = {
          match       = { macaddress = each.value.management_mac }
          "set-name"  = "lhmgmt0"
          dhcp4       = true
          dhcp6       = false
          "accept-ra" = false
        }
        cluster = {
          match       = { macaddress = each.value.cluster_mac }
          "set-name"  = "lhstorage0"
          dhcp4       = false
          dhcp6       = false
          "accept-ra" = false
          addresses   = concat(local.ipv4 ? ["${each.value.ipv4}/24"] : [], local.ipv6 ? ["${each.value.ipv6}/64"] : [])
        }
      }
    })
  }
  dynamic "device" {
    for_each = var.bootstrap_agent ? toset(["agent", "cloud-init"]) : toset([])
    content {
      name       = device.value
      type       = "disk"
      properties = { source = device.value == "agent" ? "agent:config" : "cloud-init:config" }
    }
  }
  device {
    name = "management"
    type = "nic"
    properties = {
      network        = incus_network.management.name
      hwaddr         = each.value.management_mac
      "ipv4.address" = each.value.management_ip
    }
  }
  device {
    name = "cluster"
    type = "nic"
    properties = {
      network = incus_network.cluster.name
      hwaddr  = each.value.cluster_mac
      # Routed ipvlan pod traffic legitimately uses non-node IP addresses.
      "security.ipv4_filtering" = "false"
      "security.ipv6_filtering" = "false"
    }
  }
  dynamic "device" {
    for_each = { for key, disk in local.disks : key => disk if disk.node == each.key }
    content {
      name = local.device_names[device.value.kind]
      type = "disk"
      properties = {
        pool       = var.storage_pool
        source     = incus_storage_volume.data[device.key].name
        "io.bus"   = "virtio-scsi"
        "io.cache" = "none"
      }
    }
  }
  wait_for { type = "agent" }
  wait_for { type = "cloud-init" }
  lifecycle {
    replace_triggered_by = [terraform_data.layout]
  }
}
