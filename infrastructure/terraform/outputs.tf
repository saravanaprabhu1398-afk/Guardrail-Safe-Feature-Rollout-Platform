output "namespace" {
  value = kubernetes_namespace_v1.guardrail.metadata[0].name
}

output "release_name" {
  value = helm_release.guardrail.name
}

output "release_status" {
  value = helm_release.guardrail.status
}
