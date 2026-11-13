{{/*
Chart name and fullname helpers.
*/}}
{{- define "blackbox.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "blackbox.fullname" -}}
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

{{- define "blackbox.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "blackbox.labels" -}}
helm.sh/chart: {{ include "blackbox.chart" . }}
{{ include "blackbox.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "blackbox.selectorLabels" -}}
app.kubernetes.io/name: {{ include "blackbox.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "blackbox.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) }}
{{- end }}

{{- define "blackbox.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "blackbox.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Name of the Secret used for envFrom, or empty when none is configured. */}}
{{- define "blackbox.secretName" -}}
{{- if .Values.existingSecret }}
{{- .Values.existingSecret }}
{{- else if .Values.secret.create }}
{{- printf "%s-secret" (include "blackbox.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Environment shared by the API, worker and migration Job. Keeping one definition
means a migration can never run against a different database than the pods.
*/}}
{{- define "blackbox.commonEnv" -}}
{{- range $key, $value := .Values.config }}
- name: {{ $key }}
  valueFrom:
    configMapKeyRef:
      name: {{ include "blackbox.fullname" $ }}-config
      key: {{ $key }}
{{- end }}
{{- end }}
