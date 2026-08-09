{{- define "exfil-depot.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "exfil-depot.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "exfil-depot.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "exfil-depot.labels" -}}
app.kubernetes.io/name: {{ include "exfil-depot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "exfil-depot.dbSecretName" -}}
{{- if .Values.postgres.existingSecret -}}
{{- .Values.postgres.existingSecret -}}
{{- else -}}
{{- printf "%s-db" (include "exfil-depot.fullname" .) -}}
{{- end -}}
{{- end -}}
