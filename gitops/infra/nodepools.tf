# AKS node pools for the KubeSage platform. The home lab mirrors them: the 8600G desktop plays the system pool
# and the 3500U laptop plays the lab pool, labelled with the SKU each one stands in for.
#
# The Executor agent's pull requests change vm_size, node_count and max_pods here. Merging one changes the
# desired state only: `terraform apply` against Azure is a separate step a person runs.

variable "location" {
  type    = string
  default = "eastus"
}

variable "api_server_authorized_ip_ranges" {
  type        = list(string)
  description = "CIDR blocks allowed to reach the AKS API server (override via terraform.tfvars)."
  default     = ["10.0.0.0/8"]
}

resource "azurerm_resource_group" "kubesage" {
  name     = "kubesage-demo"
  location = var.location
}

resource "azurerm_kubernetes_cluster" "kubesage" {
  name                              = "kubesage"
  location                          = azurerm_resource_group.kubesage.location
  resource_group_name               = azurerm_resource_group.kubesage.name
  dns_prefix                        = "kubesage"
  role_based_access_control_enabled = true

  api_server_access_profile {
    authorized_ip_ranges = var.api_server_authorized_ip_ranges
  }

  network_profile {
    network_plugin = "azure"
    network_policy = "azure"
  }

  default_node_pool {
    name       = "system"
    vm_size    = "Standard_D8s_v5"
    node_count = 1
    max_pods   = 110
    node_labels = {
      "kubesage.io/sku"  = "D8s_v5"
      "kubesage.io/pool" = "system"
    }
  }

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_kubernetes_cluster_node_pool" "lab" {
  name                  = "lab"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.kubesage.id
  vm_size               = "Standard_F8s_v2"
  node_count            = 1
  max_pods              = 110
  node_labels = {
    "kubesage.io/sku"  = "F8s_v2"
    "kubesage.io/pool" = "lab"
  }
}
