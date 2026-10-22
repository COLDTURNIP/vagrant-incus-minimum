locals {
  ipv4       = var.network_stack != "ipv6"
  ipv6       = var.network_stack != "ipv4"
  ipv6_first = contains(["ipv6", "dual6"], var.network_stack)
  specs = merge(
    { server = { role = "server", index = 0, cpu = var.server_cpu, memory = var.server_memory } },
    { for i in range(var.worker_count) : "worker${i + 1}" => {
      role = "worker", index = i + 1, cpu = var.worker_cpu, memory = var.worker_memory
    } }
  )
  nodes = { for key, spec in local.specs : key => merge(spec, {
    name           = "${var.project}-${key}"
    ipv4           = local.ipv4 ? cidrhost(var.cluster_ipv4_cidr, 20 + spec.index) : ""
    ipv6           = local.ipv6 ? cidrhost(var.cluster_ipv6_cidr, 20 + spec.index) : ""
    management_ip  = cidrhost(var.management_cidr, 20 + spec.index)
    management_mac = format("02:%s:%s:00:00:%02x", substr(sha256(var.project), 0, 2), substr(sha256(var.project), 2, 2), spec.index + 1)
    cluster_mac    = format("02:%s:%s:00:01:%02x", substr(sha256(var.project), 0, 2), substr(sha256(var.project), 2, 2), spec.index + 1)
    storage_v4     = cidrsubnet(var.storage_ipv4_cidr, 8, spec.index + 1)
    storage_v6     = cidrsubnet(var.storage_ipv6_cidr, 16, spec.index + 1)
  }) }
  server_url = local.ipv6_first ? "https://[${local.nodes.server.ipv6}]:6443" : "https://${local.nodes.server.ipv4}:6443"
  pod_cidrs = var.network_stack == "ipv4" ? ["10.42.0.0/16"] : (
    var.network_stack == "ipv6" ? ["fd42::/48"] : (
      local.ipv6_first ? ["fd42::/48", "10.42.0.0/16"] : ["10.42.0.0/16", "fd42::/48"]
    )
  )
  service_cidrs = var.network_stack == "ipv4" ? ["10.43.0.0/16"] : (
    var.network_stack == "ipv6" ? ["fd43::/112"] : (
      local.ipv6_first ? ["fd43::/112", "10.43.0.0/16"] : ["10.43.0.0/16", "fd43::/112"]
    )
  )
  disks = { for disk in flatten([for key, node in local.nodes : [
    for kind in concat(["k3s", "logs"], node.role == "worker" ? concat(["v1"], var.enable_v2 ? ["v2"] : []) : []) : {
      key = "${key}-${kind}", node = key, kind = kind, size = var.disk_sizes[kind]
    }
  ]]) : disk.key => disk }
  device_names = { k3s = "k3s", logs = "logs", v1 = "longhorn-v1", v2 = "longhorn-v2" }
  hostvars = { for key, node in local.nodes : node.name => {
    incus_role       = node.role
    incus_node_ips   = compact(local.ipv6_first ? [node.ipv6, node.ipv4] : [node.ipv4, node.ipv6])
    incus_ipv4       = node.ipv4
    incus_ipv6       = node.ipv6
    incus_storage_v4 = node.storage_v4
    incus_storage_v6 = node.storage_v6
    incus_disks = { for disk in values(local.disks) : disk.kind => {
      device = "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_incus_${local.device_names[disk.kind]}"
      mount  = lookup({ k3s = "/var/lib/rancher", logs = "/var/log", v1 = "/var/lib/longhorn" }, disk.kind, "")
    } if disk.node == key }
  } }
  inventory = {
    all = {
      vars = {
        ansible_connection         = "community.general.incus"
        ansible_incus_project      = var.project
        ansible_incus_remote       = var.remote
        ansible_python_interpreter = "/usr/bin/python3"
        incus_project              = var.project
        incus_remote               = var.remote
        incus_network_stack        = var.network_stack
        incus_enable_v2            = var.enable_v2
        incus_storage_network      = var.enable_storage_network
        incus_storage_ipv4_cidr    = var.storage_ipv4_cidr
        incus_storage_ipv6_cidr    = var.storage_ipv6_cidr
        incus_peers                = local.hostvars
        incus_server_url           = local.server_url
        incus_pod_cidrs            = local.pod_cidrs
        incus_service_cidrs        = local.service_cidrs
      }
      children = {
        servers = { hosts = { for name, node in local.hostvars : name => node if node.incus_role == "server" } }
        workers = { hosts = { for name, node in local.hostvars : name => node if node.incus_role == "worker" } }
        controllers = { hosts = {
          localhost = {
            ansible_connection         = "local"
            ansible_python_interpreter = "{{ ansible_playbook_python }}"
          }
        } }
      }
    }
  }
}
