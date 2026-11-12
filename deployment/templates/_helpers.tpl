{{/*
A security context (of the pod or the container) from the values, as YAML;
nothing if it is empty. A `seccompProfile` left empty (e.g., by
`seccompProfile.type: null`) is left out, like one removed with `null`, so that
the cluster's own profile applies (e.g., on CSC Rahti). Call with the context's
values.
*/}}
{{- define "pubgrade-sidecar.securityContext" -}}
{{- $context := . | default dict -}}
{{- if not $context.seccompProfile -}}
{{- $context = omit $context "seccompProfile" -}}
{{- end -}}
{{- with $context -}}
{{- toYaml . -}}
{{- end -}}
{{- end -}}
