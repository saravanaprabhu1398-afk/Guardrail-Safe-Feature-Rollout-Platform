locals {
  chart_path = coalesce(var.chart_path, abspath("${path.module}/../../deploy/helm/guardrail"))
  helm_values = {
    image = {
      repository = var.image_repository
      tag        = var.image_tag
    }
    runtime = {
      corsOrigins              = var.cors_origins
      trustedHosts             = var.trusted_hosts
      oidcIssuer               = var.oidc_issuer
      oidcAudience             = var.oidc_audience
      oidcJwksUrl              = var.oidc_jwks_url
      redpandaBootstrapServers = var.redpanda_bootstrap_servers
      redpandaSaslUsername     = var.redpanda_sasl_username
      snowflakeAccount         = var.snowflake_account
      snowflakeUser            = var.snowflake_user
    }
    existingSecrets = {
      database = {
        name   = var.database_secret_name
        urlKey = var.database_url_key
      }
      snowflake = {
        name   = var.snowflake_secret_name
        patKey = var.snowflake_pat_key
      }
      redpanda = {
        name        = var.redpanda_secret_name
        passwordKey = var.redpanda_password_key
      }
    }
    ingress = {
      enabled       = var.ingress_enabled
      className     = var.ingress_class_name
      host          = var.ui_host
      apiHost       = var.api_host
      tlsSecretName = var.tls_secret_name
    }
    networkPolicy = {
      enabled = var.network_policy_enabled
    }
  }
}

resource "kubernetes_namespace_v1" "guardrail" {
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/part-of" = "guardrail"
    }
  }
}

resource "helm_release" "guardrail" {
  name      = var.release_name
  namespace = kubernetes_namespace_v1.guardrail.metadata[0].name
  chart     = local.chart_path
  values    = [yamlencode(local.helm_values)]

  atomic          = true
  cleanup_on_fail = true
  wait            = true
  wait_for_jobs   = true
  timeout         = 900
  max_history     = 10

  depends_on = [kubernetes_namespace_v1.guardrail]
}
