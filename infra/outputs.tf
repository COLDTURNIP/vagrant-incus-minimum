output "inventory" {
  description = "Ansible inventory; contains no credentials."
  value       = local.inventory
  depends_on  = [incus_instance.node]
}
output "topology" {
  value = {
    schema        = 1
    project       = var.project
    remote        = var.remote
    storage_pool  = var.storage_pool
    network_stack = var.network_stack
    nodes         = { for key, node in local.nodes : node.name => { role = node.role, index = node.index } }
  }
  depends_on = [incus_instance.node]
}
