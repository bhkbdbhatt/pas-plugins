terraform {
  required_version = ">= 1.6"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.14"
    }
  }

  # State is held in the platform's own bucket rather than a local file: these
  # releases own ingress hostnames and TLS secrets, so losing state would orphan
  # them.
  backend "s3" {
    bucket = "pas-plugins-tfstate"
    key    = "plugins/terraform.tfstate"
    region = "us-east-1"
    encrypt = true
  }
}

provider "kubernetes" {
  host                   = var.k8s_host
  token                  = var.k8s_token
  cluster_ca_certificate = base64decode(var.k8s_ca_certificate)
}

provider "helm" {
  kubernetes {
    host                   = var.k8s_host
    token                  = var.k8s_token
    cluster_ca_certificate = base64decode(var.k8s_ca_certificate)
  }
}

locals {
  # The module, port and host for each plugin, in one place so the release list
  # and the ingress list cannot disagree.
  plugins = {
    plugin1 = {
      module = "pas_plugins.plugin1_gateway.main"
      port   = 8001
      host   = "pas.pas.example"
      tier   = "standard"
    }
    plugin2 = {
      module = "pas_plugins.plugin2_ifrs17.main"
      port   = 8002
      host   = "ifrs17.pas.example"
      tier   = "standard"
    }
    plugin3 = {
      module = "pas_plugins.plugin3_auw.main"
      port   = 8003
      host   = "underwriting.pas.example"
      tier   = "standard"
    }
    plugin4 = {
      module = "pas_plugins.plugin4_productconfig.main"
      port   = 8004
      host   = "products.pas.example"
      tier   = "standard"
    }
    plugin5 = {
      module = "pas_plugins.plugin5_embedded.main"
      port   = 8005
      host   = "distribution.pas.example"
      # Embedded distribution faces partners, so it terminates TLS directly.
      tier = "edge"
    }
    plugin6 = {
      module = "pas_plugins.plugin6_datamesh.main"
      port   = 8006
      host   = "data.pas.example"
      # The data mesh is internal; no ingress.
      tier = "internal"
    }
    plugin7 = {
      module = "pas_plugins.plugin7_blockchain.main"
      port   = 8007
      host   = "ledger.pas.example"
      # Policy records are the most heavily audited surface, so it runs larger.
      tier = "regulated"
    }
  }

  replicas = {
    standard = 2
    edge     = 3
    internal = 2
    regulated = 3
  }

  memory = {
    standard  = "1Gi"
    edge      = "1Gi"
    internal  = "2Gi"
    regulated = "2Gi"
  }
}

variable "k8s_host" {
  description = "Kubernetes API endpoint."
  type        = string
}

variable "k8s_token" {
  description = "Kubernetes API token."
  type        = string
  sensitive   = true
}

variable "k8s_ca_certificate" {
  description = "Base64-encoded cluster CA certificate."
  type        = string
}

variable "namespace" {
  description = "Namespace the plugins are released into."
  type        = string
  default     = "pas-plugins"
}

variable "image_repository" {
  description = "Plugin image repository."
  type        = string
  default     = "ghcr.io/pas-plugins/plugin"
}

variable "image_tag" {
  description = "Plugin image tag. Pin this; a floating tag makes rollbacks guesswork."
  type        = string
}

variable "oidc_issuer" {
  description = "OIDC issuer URL."
  type        = string
  default     = "https://id.pas.example"
}

variable "oidc_audience" {
  description = "OIDC audience."
  type        = string
  default     = "pas-plugins"
}

resource "kubernetes_namespace" "plugins" {
  metadata {
    name   = var.namespace
    labels = {
      "app.kubernetes.io/part-of" = "pas-plugins"
      # The plugins need a restricted security context, so the namespace default
      # enforces it rather than trusting each chart to ask.
      "pod-security.kubernetes.io/enforce" = "restricted"
      "pod-security.kubernetes.io/audit"    = "restricted"
      "pod-security.kubernetes.io/warn"     = "restricted"
    }
  }
}

resource "helm_release" "plugins" {
  for_each = local.plugins

  name       = each.key
  namespace  = kubernetes_namespace.plugins.metadata[0].name
  repository = "./deploy/helm"
  chart      = "pas-plugin"
  version    = "1.0.0"
  atomic     = true
  # `helm_release` has no create-if-absent, so wait keeps a first apply from
  # failing on a namespace that the previous resource just created.
  wait          = true
  wait_for_jobs = true
  timeout       = 600

  set {
    name  = "image.repository"
    value = var.image_repository
  }
  set {
    name  = "image.tag"
    value = var.image_tag
  }
  set {
    name  = "plugin.id"
    value = each.key
  }
  set {
    name  = "plugin.module"
    value = each.value.module
  }
  set {
    name  = "plugin.port"
    value = each.value.port
  }
  set {
    name  = "plugin.resources.limits.memory"
    value = local.memory[each.value.tier]
  }
  set {
    name  = "replicaCount"
    value = local.replicas[each.value.tier]
  }
  set {
    name  = "auth.issuer"
    value = var.oidc_issuer
  }
  set {
    name  = "auth.audience"
    value = var.oidc_audience
  }
  set {
    name  = "auth.required"
    value = "true"
  }

  # The data mesh stays internal. Exposing it would publish an ingestion API that
  # is authenticated but not designed to be on the public internet.
  set {
    name  = "ingress.enabled"
    value = each.value.tier != "internal" ? "true" : "false"
  }
  set {
    name  = "ingress.host"
    value = each.value.host
  }
  set {
    name  = "ingress.tls.enabled"
    value = "true"
  }
  set {
    name  = "ingress.tls.secretName"
    value = "${each.key}-tls"
  }

  depends_on = [kubernetes_namespace.plugins]
}

output "plugin_endpoints" {
  description = "Ingress host for each externally exposed plugin."
  value = {
    for key, plugin in local.plugins : key => (
      plugin.tier == "internal"
      ? "internal only (${plugin.module})"
      : "https://${plugin.host}"
    )
  }
}

output "namespace" {
  description = "Namespace the plugins were released into."
  value       = kubernetes_namespace.plugins.metadata[0].name
}