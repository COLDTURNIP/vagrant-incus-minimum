mock_provider "incus" {}

run "ipv4" {
  command = plan
  variables { network_stack = "ipv4" }
  assert {
    condition     = length(incus_instance.node) == 4 && length(incus_storage_volume.data) == 14
    error_message = "Expected one server, three workers and dedicated system/V1/V2 disks."
  }
  assert {
    condition     = output.inventory.all.vars.incus_server_url == "https://172.28.156.20:6443" && length(output.inventory.all.vars.incus_pod_cidrs) == 1 && output.inventory.all.vars.incus_pod_cidrs[0] == "10.42.0.0/16"
    error_message = "IPv4 endpoint and pod CIDRs must match."
  }
  assert {
    condition     = output.inventory.all.children.workers.hosts["lh-incus-worker1"].incus_disks.v1.serial == "incus_longhorn--v1" && endswith(output.inventory.all.children.workers.hosts["lh-incus-worker1"].incus_disks.v2.device, "_incus_longhorn--v2")
    error_message = "Incus escapes hyphens in disk serials; inventory must use the actual by-id identity."
  }
}
run "ipv6" {
  command = plan
  variables { network_stack = "ipv6" }
  assert {
    condition     = output.inventory.all.vars.incus_server_url == "https://[fd00:1c05:156::14]:6443" && output.inventory.all.children.workers.hosts["lh-incus-worker1"].incus_ipv4 == ""
    error_message = "IPv6-only cluster addresses must not include IPv4."
  }
}
run "dual" {
  command = plan
  variables {
    network_stack          = "dual"
    enable_storage_network = true
  }
  assert {
    condition     = output.inventory.all.vars.incus_pod_cidrs[0] == "10.42.0.0/16" && output.inventory.all.vars.incus_storage_network
    error_message = "Dual-stack must be IPv4-first and preserve the storage toggle."
  }
}
run "dual6" {
  command = plan
  variables { network_stack = "dual6" }
  assert {
    condition     = output.inventory.all.vars.incus_pod_cidrs[0] == "fd42::/48" && startswith(output.inventory.all.vars.incus_server_url, "https://[")
    error_message = "IPv6-first endpoint and primary pod family must agree."
  }
}
run "v1_only" {
  command = plan
  variables {
    enable_v2    = false
    worker_count = 1
  }
  assert {
    condition     = length(incus_storage_volume.data) == 5 && !contains(keys(output.inventory.all.children.workers.hosts["lh-incus-worker1"].incus_disks), "v2")
    error_message = "V1-only topology must not allocate or advertise V2 disks."
  }
}
run "reject_default_project" {
  command = plan
  variables { project = "default" }
  expect_failures = [var.project]
}
