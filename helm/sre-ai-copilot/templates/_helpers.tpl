{{/*
Expand the name of the chart.
*/}}
{{- define "sre-ai-copilot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "sre-ai-copilot.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart label string used by the selector labels.
*/}}
{{- define "sre-ai-copilot.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "sre-ai-copilot.labels" -}}
helm.sh/chart: {{ include "sre-ai-copilot.chart" . }}
{{ include "sre-ai-copilot.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: sre-ai-copilot
{{- end }}

{{/*
Selector labels (shared base — component added per-workload)
*/}}
{{- define "sre-ai-copilot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sre-ai-copilot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the ServiceAccount
*/}}
{{- define "sre-ai-copilot.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- include "sre-ai-copilot.fullname" . }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the Secret with app credentials (используется во ВСЕХ envFrom:
api / worker / beat / job-migrate — точка подмены одна, здесь).

Два пути, см. values.yaml → secrets:
  1. secrets.existingSecret — ПРЕДПОЧТИТЕЛЬНЫЙ: Secret создан заранее и вне
     чарта (SealedSecrets / ExternalSecrets / руками), чарт только ссылается
     на него и НИЧЕГО не рендерит → ключи не попадают ни в helm release
     Secret (sh.helm.release.v1.*), ни в shell history через --set.
  2. пусто (дефолт) — чарт рендерит templates/secret.yaml из values.
     Небезопасный путь, оставлен для kind/minikube; см. warning там же.
*/}}
{{- define "sre-ai-copilot.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- printf "%s-secrets" (include "sre-ai-copilot.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Standard pod security context used by API and worker
*/}}
{{- define "sre-ai-copilot.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{/*
Standard container security context used by API and worker
*/}}
{{- define "sre-ai-copilot.containerSecurityContext" -}}
readOnlyRootFilesystem: true
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
{{- end }}
