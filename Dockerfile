##### TOOLS #####
## uv, to export the locked requirements (`uv.lock`) for the image build, and
## to install them; mounted into the step that uses it only, so that the image
## doesn't contain it (the FOCA image has no pip). Pinned by digest; Dependabot
## updates the tag and digest together
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

##### BASE IMAGE #####
## Pinned by digest, so that rebuilds use the same base image. FOCA is bumped
## by hand, the base image and `foca` in `pyproject.toml` and `uv.lock`
## together (`.github/dependabot.yml`; `tests/unit/test_image.py` checks that
## they match, and that the Python version is the one `pyproject.toml`
## requires)
FROM elixircloud/foca:0.21.0-py3.13@sha256:ba79033cab60dd79419c2c44f90000f4fb9aa8d495ea09c2798a7348516bd84f

##### METADATA #####
## OCI annotations; images built from the FOCA image inherit its own, so all
## static ones are set here. CI's `docker/metadata-action` sets the version,
## revision and creation date and overrides title, description, URLs and
## licenses with the repository's metadata
LABEL org.opencontainers.image.title="Pubgrade Sidecar"
LABEL org.opencontainers.image.description="Updates the image tags of Kubernetes Deployments when Pubgrade notifies it"
LABEL org.opencontainers.image.url="https://github.com/elixir-cloud-aai/Pubgrade-Sidecar"
LABEL org.opencontainers.image.source="https://github.com/elixir-cloud-aai/Pubgrade-Sidecar"
LABEL org.opencontainers.image.documentation="https://pubgrade.readthedocs.io/en/latest/sidecar/"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.authors="ELIXIR On-Cloud <cloud-service@elixir-europe.org>"
LABEL org.opencontainers.image.vendor="ELIXIR On-Cloud"

## Install dependencies first, so that code changes do not reinstall them; the
## FOCA image runs as non-root user. Only the locked ones are installed: the
## runtime dependencies (`project.dependencies` of `pyproject.toml`; `--no-dev`
## leaves out the `dev` dependency group) and all their own dependencies, at
## the exact versions of `uv.lock`, checked against its hashes, as wheels
## only, and without resolving anything else (`--no-deps`), and compiled, as
## the root file system of deployments may be read-only. `--frozen` takes the
## lock as is, unchecked against `pyproject.toml` (CI's `uv lock --check`
## checks it). uv installs them into the Python of the base image
## (`--system`); packages that the base image already has at the locked
## version, such as FOCA, are kept as they are. Then uv checks that the
## installed packages meet each other's requirements, as nothing was resolved
USER root
COPY pyproject.toml uv.lock /tmp/src/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  export UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never \
  && uv export --frozen --no-dev --no-emit-project \
    --project /tmp/src --output-file /tmp/src/requirements.txt \
  && uv pip install --system --require-hashes --only-binary :all: --no-deps \
    --compile-bytecode -r /tmp/src/requirements.txt \
  && uv pip check --system \
  && rm -rf /tmp/src /tmp/uv-*.lock

## Copy the app, which runs from its source directory, owned by root and
## read-only for the app's user (any user ID, e.g., on OpenShift), also if
## the build context has restrictive file modes
COPY ./app /app/app
RUN chmod -R a+rX,go-w /app/app && python -m compileall -q /app/app
WORKDIR /app/app
USER 1000

## Serve with Gunicorn (FOCA's settings for containers, `foca.gunicorn_config`:
## port 8080, one worker with threads; override them via `GUNICORN_CMD_ARGS`,
## but keep one worker, as the sidecar keeps its deliveries in memory). The
## worker creates the app, and Gunicorn exits if that fails.
## `python -m pubgrade_sidecar.app` runs Flask's development server instead
CMD ["gunicorn", "--config", "python:foca.gunicorn_config", \
  "pubgrade_sidecar.app:create_app()"]
