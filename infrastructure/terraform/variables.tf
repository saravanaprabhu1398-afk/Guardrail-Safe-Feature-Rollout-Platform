variable "kubeconfig_path" {
  description = "Path to the kubeconfig used by the Kubernetes and Helm providers."
  type        = string
  default     = "~/.kube/config"
}

variable "kubeconfig_context" {
  description = "Kubeconfig context for the target cluster."
  type        = string
}

variable "namespace" {
  description = "Namespace in which Guardrail is installed."
  type        = string
  default     = "guardrail"
}

variable "release_name" {
  type    = string
  default = "guardrail"
}

variable "chart_path" {
  description = "Override for the local Guardrail Helm chart path."
  type        = string
  default     = null
  nullable    = true
}

variable "image_repository" {
  type = string
}

variable "image_tag" {
  description = "Immutable application image tag, normally the Git SHA."
  type        = string
  validation {
    condition     = length(var.image_tag) > 0 && var.image_tag != "latest"
    error_message = "image_tag must be an immutable non-latest tag."
  }
}

variable "database_secret_name" {
  type    = string
  default = "guardrail-database"
}

variable "database_url_key" {
  type    = string
  default = "database-url"
}

variable "snowflake_secret_name" {
  type    = string
  default = "guardrail-snowflake"
}

variable "snowflake_pat_key" {
  type    = string
  default = "pat"
}

variable "oidc_issuer" { type = string }
variable "oidc_audience" { type = string }
variable "oidc_jwks_url" { type = string }
variable "cors_origins" { type = string }
variable "trusted_hosts" { type = string }
variable "redpanda_bootstrap_servers" { type = string }
variable "redpanda_sasl_username" { type = string }
variable "redpanda_secret_name" {
  type    = string
  default = "guardrail-redpanda"
}
variable "redpanda_password_key" {
  type    = string
  default = "sasl-password"
}
variable "snowflake_account" { type = string }
variable "snowflake_user" { type = string }

variable "ingress_enabled" {
  type    = bool
  default = false
}

variable "ingress_class_name" {
  type    = string
  default = ""
}

variable "ui_host" {
  type    = string
  default = "guardrail.example.com"
}

variable "api_host" {
  type    = string
  default = "guardrail-api.example.com"
}

variable "tls_secret_name" {
  type    = string
  default = "guardrail-tls"
}

variable "network_policy_enabled" {
  type    = bool
  default = false
}
