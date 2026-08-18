# Guardrail Terraform root

This root creates a namespace and installs the local production Helm chart. It expects an existing
Kubernetes cluster and pre-created database/Snowflake secrets. Sensitive values never enter
Terraform inputs or state.

```bash
cp terraform.tfvars.example production.auto.tfvars
terraform init
terraform fmt -check
terraform validate
terraform apply -target=kubernetes_namespace_v1.guardrail
# Wait for the external secret controller to materialize all three Guardrail secrets.
terraform plan -out=guardrail.tfplan
terraform apply guardrail.tfplan
```

See [`docs/runbooks/production-deployment.md`](../../docs/runbooks/production-deployment.md)
for prerequisites, acceptance, and rollback rules.
