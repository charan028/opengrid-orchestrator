{{/* Shared names, labels, images and pod fragments of the OpenGrid chart. */}}

{{- define "og.fullname" -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "og.labels" -}}
app.kubernetes.io/name: opengrid
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: opengrid
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
{{- end -}}

{{- define "og.selector" -}}
app.kubernetes.io/name: opengrid
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "og.tag" -}}
{{- $tag := required "image.tag is required (install.sh sets it to the git short sha)" .Values.image.tag -}}
{{- if eq $tag "latest" -}}{{- fail "image.tag must be immutable; \"latest\" is refused" -}}{{- end -}}
{{- $tag -}}
{{- end -}}

{{- define "og.image" -}}
{{- $repo := index .root.Values.image .which -}}
{{- if .root.Values.image.registry -}}
{{- printf "%s/%s:%s" (trimSuffix "/" .root.Values.image.registry) $repo (include "og.tag" .root) -}}
{{- else -}}
{{- printf "%s:%s" $repo (include "og.tag" .root) -}}
{{- end -}}
{{- end -}}

{{- define "og.dbHost" -}}
{{- if .Values.postgres.external.enabled -}}
{{- required "postgres.external.host is required when postgres.external.enabled" .Values.postgres.external.host -}}
{{- else -}}
{{- printf "%s-postgres" (include "og.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "og.dbPort" -}}
{{- if .Values.postgres.external.enabled -}}{{ .Values.postgres.external.port }}{{- else -}}{{ .Values.postgres.port }}{{- end -}}
{{- end -}}

{{/* The orchestrator's database environment (opengrid.platform.db.build_dsn + bootstrap_from_scratch.sh). */}}
{{- define "og.dbEnv" -}}
- name: OG_DB
  value: {{ .Values.postgres.database | quote }}
- name: OG_DB_USER
  value: {{ .Values.postgres.role | quote }}
- name: OG_DB_HOST
  value: {{ include "og.dbHost" . | quote }}
- name: OG_DB_PORT
  value: {{ include "og.dbPort" . | quote }}
- name: OG_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.db }}
      key: OG_DB_PASSWORD
{{- end -}}

{{- define "og.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .Values.podSecurity.runAsUser }}
runAsGroup: {{ .Values.podSecurity.runAsGroup }}
fsGroup: {{ .Values.podSecurity.fsGroup }}
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "og.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{- define "og.imagePullSecrets" -}}
{{- with .Values.image.pullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end -}}

{{/* Writable scratch for a read-only root: the rendered config (/run/og), /tmp and /var/lib/opengrid. */}}
{{- define "og.runVolumeMounts" -}}
- name: run
  mountPath: /run/og
- name: tmp
  mountPath: /tmp
- name: overrides
  mountPath: /etc/opengrid-k8s
  readOnly: true
{{- end -}}

{{- define "og.runVolumes" -}}
- name: run
  emptyDir:
    medium: Memory
    sizeLimit: 64Mi
- name: tmp
  emptyDir:
    sizeLimit: 512Mi
- name: overrides
  configMap:
    name: {{ include "og.fullname" . }}-config
{{- end -}}

{{/* initContainer: wait until every migration in the image is applied (the migrate Job). */}}
{{- define "og.waitSchema" -}}
- name: wait-schema
  image: {{ include "og.image" (dict "root" . "which" "orchestrator") }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  args: ["wait-schema"]
  env:
    {{- include "og.dbEnv" . | nindent 4 }}
  securityContext:
    {{- include "og.containerSecurityContext" . | nindent 4 }}
  resources:
    requests: {cpu: 20m, memory: 64Mi}
    limits: {memory: 256Mi}
  volumeMounts:
    {{- include "og.runVolumeMounts" . | nindent 4 }}
{{- end -}}
