##### PUBGRADE CANARY #####
## A tiny web server whose only job is to be upgraded by Pubgrade: it serves
## `/version.json` with the version and commit it was built from, so that the
## workflow `Canary` (`.github/workflows/canary.yml`) can see when a site has
## rolled out a new tag. See `docs/canary.md`.
##
## BusyBox's `httpd`, pinned by digest (Dependabot updates the tag and digest
## together); no code of our own, no shell needed at runtime.
FROM busybox:1.38.0-musl@sha256:80e38f9a1b39188b8bdb28879178edb322a4402b081a9aedda1924609decd6f6

## Version (`X.Y.Z`) and commit (hex) of the build; the workflow sets both.
## They are checked, so that nothing else ends up in the JSON
ARG VERSION=0.0.0
ARG COMMIT=unknown

LABEL org.opencontainers.image.title="Pubgrade canary"
LABEL org.opencontainers.image.description="Serves its own version, for testing automatic upgrades with Pubgrade"
LABEL org.opencontainers.image.source="https://github.com/elixir-cloud-aai/Pubgrade-Sidecar"
LABEL org.opencontainers.image.documentation="https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/blob/dev/docs/canary.md"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.vendor="ELIXIR On-Cloud"

## (each a single line: `grep -x` checks lines, not the whole value)
RUN [ "$(printf '%s' "${VERSION}${COMMIT}" | tr -d -c '\n' | wc -c)" -eq 0 ] \
    && echo "${VERSION}" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+' \
    && echo "${COMMIT}" | grep -Eqx '[0-9a-f]{7,40}|unknown' \
    && mkdir -p /www \
    && printf '{"version": "%s", "commit": "%s"}\n' "${VERSION}" "${COMMIT}" \
       > /www/version.json \
    && chmod -R a+rX /www

## Any user can run it (OpenShift assigns one per namespace); port 8080 needs
## no privileges
USER 65534:0
EXPOSE 8080
## In the foreground, logging each request to standard error
CMD ["httpd", "-f", "-v", "-p", "8080", "-h", "/www"]
