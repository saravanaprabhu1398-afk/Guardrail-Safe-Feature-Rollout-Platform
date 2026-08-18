{{- define "guardrail.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "guardrail.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "guardrail.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "guardrail.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/name: {{ include "guardrail.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "guardrail.selectorLabels" -}}
app.kubernetes.io/name: {{ include "guardrail.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "guardrail.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "guardrail.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- required "serviceAccount.name is required when create=false" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "guardrail.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{- define "guardrail.databaseEnv" -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ required "existingSecrets.database.name is required" .Values.existingSecrets.database.name }}
      key: {{ required "existingSecrets.database.urlKey is required" .Values.existingSecrets.database.urlKey }}
{{- end }}

{{- define "guardrail.redpandaSecretEnv" -}}
- name: REDPANDA_SASL_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ required "existingSecrets.redpanda.name is required" .Values.existingSecrets.redpanda.name }}
      key: {{ required "existingSecrets.redpanda.passwordKey is required" .Values.existingSecrets.redpanda.passwordKey }}
{{- end }}

{{- define "guardrail.snowflakeVolume" -}}
- name: snowflake-pat
  secret:
    secretName: {{ required "existingSecrets.snowflake.name is required" .Values.existingSecrets.snowflake.name }}
    defaultMode: 0440
    items:
      - key: {{ required "existingSecrets.snowflake.patKey is required" .Values.existingSecrets.snowflake.patKey }}
        path: pat
{{- end }}

{{- define "guardrail.containerSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
readOnlyRootFilesystem: true
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end }}
