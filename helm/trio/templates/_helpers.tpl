{{- define "trio.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "trio.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "trio.selectorLabels" -}}
app.kubernetes.io/name: {{ include "trio.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "trio.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{ include "trio.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "trio.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "trio.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "trio.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}
{{- end }}

{{/* The Secret mounted under /run/secrets/trio, if there is one. */}}
{{- define "trio.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else if .Values.secrets.create }}
{{- include "trio.fullname" . }}
{{- end }}
{{- end }}

{{/* A volume for one of the three writable directories: a claim, or an emptyDir. */}}
{{- define "trio.volume" -}}
{{- $root := index . 0 -}}
{{- $name := index . 1 -}}
{{- $spec := index $root.Values.persistence $name -}}
- name: {{ $name }}
{{- if $spec.enabled }}
  persistentVolumeClaim:
    claimName: {{ default (printf "%s-%s" (include "trio.fullname" $root) $name) $spec.existingClaim }}
{{- else }}
  emptyDir: {}
{{- end }}
{{- end }}
