{{/*
MIT License

Copyright (c) 2026 Shane & Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
*/}}
{{/*
Expand the name of the chart.
*/}}
{{- define "technitium.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Effective administrator Secret name and password key. */}}
{{- define "technitium.adminSecretName" -}}
{{- $config := .Values.config | default dict -}}
{{- $existing := $config.existingSecret | default "" -}}
{{- if $existing -}}{{- $existing -}}{{- else -}}{{- printf "%s-admin" (include "technitium.fullname" .) -}}{{- end -}}
{{- end }}

{{- define "technitium.adminPasswordKey" -}}
{{- $config := .Values.config | default dict -}}
{{- $key := "password" -}}
{{- if $config.existingSecret -}}
{{- $key = required "config.passwordKey must not be empty when config.existingSecret is set" $config.passwordKey -}}
{{- if not (regexMatch "^[-._a-zA-Z0-9]+$" $key) -}}
{{- fail "config.passwordKey must contain only letters, numbers, dots, underscores, and hyphens" -}}
{{- end -}}
{{- end -}}
{{- $key -}}
{{- end }}

{{/* Effective primary node Secret settings used by secondary cluster Jobs/NOTES. */}}
{{- define "technitium.primaryAdminSecretName" -}}
{{- $cluster := .Values.cluster | default dict -}}
{{- if $cluster.primaryAdminSecretName -}}
{{- $cluster.primaryAdminSecretName -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name $cluster.primaryReleaseName -}}
{{- printf "%s-admin" ($cluster.primaryReleaseName | trunc 63 | trimSuffix "-") -}}
{{- else -}}
{{- printf "%s-%s-admin" $cluster.primaryReleaseName $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "technitium.primaryAdminPasswordKey" -}}
{{- $cluster := .Values.cluster | default dict -}}
{{- $key := required "cluster.primaryAdminPasswordKey must not be empty" $cluster.primaryAdminPasswordKey -}}
{{- if not (regexMatch "^[-._a-zA-Z0-9]+$" $key) -}}
{{- fail "cluster.primaryAdminPasswordKey must contain only letters, numbers, dots, underscores, and hyphens" -}}
{{- end -}}
{{- $key -}}
{{- end }}

{{/*
Create a default fully qualified app name. Managed mode reserves room for
member resource suffixes and hashes long names to avoid collisions.
*/}}
{{- define "technitium.fullname" -}}
{{- $raw := "" -}}
{{- if .Values.fullnameOverride }}
{{- $raw = .Values.fullnameOverride -}}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- $raw = .Release.Name -}}
{{- else }}
{{- $raw = printf "%s-%s" .Release.Name $name -}}
{{- end }}
{{- end }}
{{- if and (.Values.cluster.managed | default dict).enabled (gt (len $raw) 44) -}}
{{- printf "%s-%s" ($raw | trunc 35 | trimSuffix "-") ($raw | sha256sum | trunc 8) -}}
{{- else -}}
{{- $raw | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end }}

{{- define "technitium.managedEnabled" -}}
{{- if (.Values.cluster.managed | default dict).enabled -}}true{{- end -}}
{{- end -}}

{{- define "technitium.managedValidate" -}}
{{- $m := .Values.cluster.managed | default dict -}}
{{- if and (not $m.enabled) .Release.IsUpgrade -}}
{{- range (lookup "apps/v1" "StatefulSet" .Release.Namespace "").items -}}
{{- if eq (index (.metadata.labels | default dict) "app.kubernetes.io/instance") $.Release.Name -}}
{{- fail "managed clustering cannot be disabled while its StatefulSet exists; migrate members deliberately first" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if $m.enabled -}}
{{- $tls := $m.tls | default dict -}}
{{- if not (kindIs "map" $tls) -}}{{ fail "cluster.managed.tls must be a map" }}{{- end -}}
{{- if and (hasKey $tls "verify") (not (kindIs "bool" $tls.verify)) -}}{{ fail "cluster.managed.tls.verify must be a boolean" }}{{- end -}}
{{- if and $tls.caSecretName $tls.caConfigMapName -}}{{ fail "set only one of cluster.managed.tls.caSecretName and caConfigMapName" }}{{- end -}}
{{- range $field := list "caSecretName" "caConfigMapName" -}}
{{- $value := get $tls $field -}}
{{- if and $value (not (regexMatch "^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$" (toString $value))) -}}{{ fail (printf "cluster.managed.tls.%s must be a valid resource name" $field) }}{{- end -}}
{{- end -}}
{{- if or $tls.caSecretName $tls.caConfigMapName -}}
{{- if not (regexMatch "^[a-zA-Z0-9._-]+$" (toString ($tls.caKey | default "ca.crt"))) -}}{{ fail "cluster.managed.tls.caKey must be a valid Secret key" }}{{- end -}}
{{- end -}}
{{- if and (hasKey $m "bootstrapHttp") (not (kindIs "bool" $m.bootstrapHttp)) -}}{{ fail "cluster.managed.bootstrapHttp must be a boolean" }}{{- end -}}
{{- if hasKey $m "updateTargetOrdinal" -}}{{ fail "cluster.managed.updateTargetOrdinal was removed; the primary observer now selects the primary Service endpoint" }}{{- end -}}
{{- if not .Values.cluster.enabled -}}{{ fail "cluster.enabled must be true when cluster.managed.enabled is true" }}{{- end -}}
{{- if not .Values.cluster.autoJoin -}}{{ fail "cluster.autoJoin must be true when cluster.managed.enabled is true" }}{{- end -}}
{{- if and .Values.bootstrap.enabled (or .Values.bootstrap.zones .Values.bootstrap.reverseZones) -}}{{ fail "bootstrap zones are not supported in managed mode; enroll zones after the cluster is verified" }}{{- end -}}
{{- if .Values.cluster.primaryReleaseName -}}{{ fail "cluster.primaryReleaseName is incompatible with cluster.managed.enabled" }}{{- end -}}
{{- if ne (include "technitium.persistenceEnabled" .) "true" -}}{{ fail "cluster.managed requires persistence.enabled=true" }}{{- end -}}
{{- if (.Values.persistence | default dict).existingClaim -}}{{ fail "cluster.managed cannot use persistence.existingClaim; each StatefulSet member needs its own claim" }}{{- end -}}
{{- if not (regexMatch "^[0-9]$" (toString $m.secondaries)) -}}{{ fail "cluster.managed.secondaries must be an integer from 0 to 9" }}{{- end -}}
{{- end -}}
{{- end -}}

{{- define "technitium.managedMemberName" -}}
{{- printf "%s-cluster" (include "technitium.fullname" .) -}}
{{- end -}}

{{- define "technitium.managedHookName" -}}
{{- printf "technitium-managed-%s-%s" (.Release.Name | trunc 24 | trimSuffix "-") (printf "%s/%s" .Release.Namespace .Release.Name | sha256sum | trunc 8) -}}
{{- end -}}

{{- define "technitium.managedTlsVerify" -}}
{{- $tls := .Values.cluster.managed.tls | default dict -}}
{{- if hasKey $tls "verify" -}}{{ $tls.verify }}{{- else -}}true{{- end -}}
{{- end -}}

{{- define "technitium.managedTlsEnv" -}}
{{- $tls := .Values.cluster.managed.tls | default dict -}}
- name: VERIFY_TLS
  value: {{ include "technitium.managedTlsVerify" . | quote }}
{{- if or $tls.caSecretName $tls.caConfigMapName }}
- name: CA_FILE
  value: /peer-ca/ca.crt
{{- end -}}
{{- end -}}

{{- define "technitium.managedCaMount" -}}
{{- $tls := .Values.cluster.managed.tls | default dict -}}
{{- if or $tls.caSecretName $tls.caConfigMapName -}}
- name: managed-peer-ca
  mountPath: /peer-ca
  readOnly: true
{{- end -}}
{{- end -}}

{{- define "technitium.managedCaVolume" -}}
{{- $tls := .Values.cluster.managed.tls | default dict -}}
{{- if or $tls.caSecretName $tls.caConfigMapName -}}
- name: managed-peer-ca
{{- if $tls.caSecretName }}
  secret:
    secretName: {{ $tls.caSecretName | quote }}
{{- else }}
  configMap:
    name: {{ $tls.caConfigMapName | quote }}
{{- end }}
    items:
      - key: {{ $tls.caKey | default "ca.crt" | quote }}
        path: ca.crt
{{- end -}}
{{- end -}}


{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "technitium.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "technitium.labels" -}}
helm.sh/chart: {{ include "technitium.chart" . }}
{{ include "technitium.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- $cluster := include "technitium.clusterLabels" . | trim -}}
{{- if $cluster }}
{{ $cluster }}
{{- end }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "technitium.selectorLabels" -}}
app.kubernetes.io/name: {{ include "technitium.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Cluster discovery labels. Emitted only when cluster.enabled is true so
sibling releases can be listed with:
  kubectl get pods -l technitium.io/cluster-domain=<domain>
*/}}
{{- define "technitium.clusterLabels" -}}
{{- if .Values.cluster.enabled }}
{{- $domain := required "cluster.domain is required when cluster.enabled" .Values.cluster.domain }}
technitium.io/cluster-domain: {{ $domain | replace "." "-" | trunc 63 | trimSuffix "-" | quote }}
{{- if not (.Values.cluster.managed | default dict).enabled }}
technitium.io/cluster-role: {{ ternary "secondary" "primary" (ne (.Values.cluster.primaryReleaseName | toString) "") | quote }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Persistence toggle (issue #43). `persistence` defaults to `{}`, so an unset
`enabled` means true; only an explicit `false` turns the PVC off. Returns
"true" or "".
*/}}
{{- define "technitium.persistenceEnabled" -}}
{{- $p := .Values.persistence | default dict -}}
{{- if or (not (hasKey $p "enabled")) $p.enabled -}}
true
{{- end -}}
{{- end }}

{{/*
Zone bootstrap sidecar toggle (issue #43): on when bootstrap.enabled and at
least one zone is listed, and never on a cluster secondary (zones created on
the primary reach secondaries through the cluster catalog). Returns "true"
or "".
*/}}
{{- define "technitium.bootstrapEnabled" -}}
{{- $b := .Values.bootstrap | default dict -}}
{{- $secondary := and .Values.cluster.enabled (ne (.Values.cluster.primaryReleaseName | toString) "") -}}
{{- if and $b.enabled (or $b.zones $b.reverseZones) (not $secondary) (ne (include "technitium.managedEnabled" .) "true") -}}
true
{{- end -}}
{{- end }}

{{/*
User-supplied Service labels (issue #32), for label-selecting controllers
such as Cilium LB-IPAM `serviceSelector`. Merges `service.labels` (every
Service) with the per-Service map passed as `labels` (per-Service wins).
Keys the chart already sets on the Service are rejected rather than
silently dropped or duplicated. Values are rendered as quoted strings,
since Kubernetes label values must be strings.
Call with: dict "root" $ "labels" .Values.service.<svc>.labels
*/}}
{{- define "technitium.serviceExtraLabels" -}}
{{- $root := .root -}}
{{- $reserved := include "technitium.labels" $root | fromYaml -}}
{{- $_ := set $reserved "app.kubernetes.io/component" "" -}}
{{- $extra := dict -}}
{{- range $k, $v := ($root.Values.service.labels | default dict) }}
{{- $_ := set $extra $k $v }}
{{- end }}
{{- range $k, $v := (.labels | default dict) }}
{{- $_ := set $extra $k $v }}
{{- end }}
{{- range $k, $v := $extra }}
{{- if hasKey $reserved $k }}
{{- fail (printf "service labels: %q is set by the chart and cannot be overridden" $k) }}
{{- end }}
{{ $k }}: {{ $v | toString | quote }}
{{- end }}
{{- end }}

{{/*
Effective HTTPS toggle. Clustering needs DANE-EE TLS on the web service, so
cluster.enabled + cluster.autoHttps implies HTTPS even if the user did not
explicitly opt in via config.webServiceEnableHttps.
*/}}
{{- define "technitium.httpsEnabled" -}}
{{- if or .Values.config.webServiceEnableHttps (and .Values.cluster.enabled .Values.cluster.autoHttps) -}}
true
{{- end -}}
{{- end }}

{{/*
Effective self-signed cert toggle. Same reasoning as httpsEnabled.
*/}}
{{- define "technitium.selfSignedCertEnabled" -}}
{{- if or .Values.config.webServiceUseSelfSignedCert (and .Values.cluster.enabled .Values.cluster.autoHttps) -}}
true
{{- end -}}
{{- end }}

{{/*
In-cluster web URL for an arbitrary release name in this release's namespace.
Usage:
  {{ include "technitium.webUrl" (dict "root" . "release" "my-primary") }}
*/}}
{{- define "technitium.webUrl" -}}
{{- $root := .root -}}
{{- $name := default $root.Chart.Name $root.Values.nameOverride -}}
{{- $full := printf "%s-%s" .release $name | trunc 63 | trimSuffix "-" -}}
{{- if eq (include "technitium.httpsEnabled" $root) "true" -}}
https://{{ $full }}-web.{{ $root.Release.Namespace }}.svc.cluster.local:{{ $root.Values.ports.webHttps | default 53443 }}
{{- else -}}
http://{{ $full }}-web.{{ $root.Release.Namespace }}.svc.cluster.local:{{ $root.Values.ports.webHttp | default 5380 }}
{{- end -}}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "technitium.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "technitium.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
