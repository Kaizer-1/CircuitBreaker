{{/*
The service's name. Under an alias in the umbrella chart, .Chart.Name IS the alias (e.g. order-service), so every
object is named after its service and Services keep the same DNS names as in Docker Compose.
*/}}
{{- define "service.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{/* Render this service? global.only lets Phase 6 render the umbrella for one service per Argo CD Application. */}}
{{- define "service.enabled" -}}
{{- $only := (.Values.global).only | default "" -}}
{{- if and .Values.enabled (or (eq $only "") (eq $only .Chart.Name)) -}}true{{- end -}}
{{- end -}}

{{- define "service.labels" -}}
app.kubernetes.io/name: {{ include "service.name" . }}
app.kubernetes.io/part-of: circuitbreaker
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ include "service.tag" . | quote }}
circuitbreaker.io/environment: {{ .Values.global.environment | quote }}
{{- end -}}

{{- define "service.selector" -}}
app.kubernetes.io/name: {{ include "service.name" . }}
{{- end -}}

{{- define "service.tag" -}}
{{- .Values.image.tag | default .Values.global.image.tag | default "dev" -}}
{{- end -}}

{{- define "service.image" -}}
{{- $repo := .Values.image.repository | default (printf "circuitbreaker/%s" .Chart.Name) -}}
{{- with .Values.global.image.registry -}}{{ . }}/{{- end -}}{{ $repo }}:{{ include "service.tag" . }}
{{- end -}}
