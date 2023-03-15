{{/*
Expand the name of the chart.
*/}}
{{- define "ceph-backup.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "ceph-backup.fullname" -}}
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
Create chart name and version as used by the chart label.
*/}}
{{- define "ceph-backup.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "ceph-backup.labels" -}}
helm.sh/chart: {{ include "ceph-backup.chart" . }}
app.kubernetes.io/name: {{ include "ceph-backup.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Scheduler labels
*/}}
{{- define "ceph-backup.scheduler.labels" -}}
helm.sh/chart: {{ include "ceph-backup.chart" . }}
{{ include "ceph-backup.scheduler.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Scheduler selector labels
*/}}
{{- define "ceph-backup.scheduler.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ceph-backup.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: scheduler
{{- end }}

{{/*
Metrics labels
*/}}
{{- define "ceph-backup.metrics.labels" -}}
helm.sh/chart: {{ include "ceph-backup.chart" . }}
{{ include "ceph-backup.metrics.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Metrics selector labels
*/}}
{{- define "ceph-backup.metrics.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ceph-backup.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: metrics
{{- end }}
