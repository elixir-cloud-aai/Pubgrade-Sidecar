##### TOOLS #####
## uv, to export the locked requirements (`uv.lock`) for the image build;
## mounted into the step that exports them only, so that the image doesn't
## contain it. Pinned by digest; Dependabot updates the tag and digest together
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

##### BASE IMAGE #####
## Pinned by digest, so that rebuilds use the same base image. FOCA is bumped
## by hand, the base image and `foca` in `pyproject.toml` together
## (`.github/dependabot.yml`)
FROM elixircloud/foca:v0.7.0-py3.9@sha256:e96f018a75b878ab9fe8de63b756786d808fb0706898e02a998b562743340da3


##### METADATA #####

LABEL software="Pubgrade"
LABEL software.description="Pubgrade is a decoupled, publish-subscribe-based continuous integration (CI) and continuous delivery (CD) microservice that allows developers to notify deploments of available updates."
LABEL software.website="https://github.com/elixir-cloud-aai/Pubgrade"
LABEL software.license="https://spdx.org/licenses/Apache-2.0"
LABEL maintainer="akash2237778@gmail.com"
LABEL maintainer.organisation="ELIXIR Cloud & AAI"

RUN groupadd -r pubgrade --gid 1005 && useradd -d /home/pubgrade -ms /bin/bash -r -g pubgrade pubgrade --uid 1005

## Install dependencies first, so that code changes do not reinstall them. Only
## the locked ones are installed: the runtime dependencies
## (`project.dependencies` of `pyproject.toml`; `--no-dev` leaves out the `dev`
## dependency group) and all their own dependencies, at the exact versions of
## `uv.lock`, checked against its hashes, and without resolving anything else
## (`--no-deps`). `--frozen` takes the lock as is, unchecked against
## `pyproject.toml` (CI's `uv lock --check` checks it). Packages that the base
## image already has at the locked version are kept. Wheels only, except
## pymongo: its locked version (FOCA's pin) has no wheel for Python 3.9, and
## the base image has it already (FOCA 0.7 pins the same version)
COPY pyproject.toml uv.lock /tmp/src/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
  UV_NO_CACHE=1 uv export --frozen --no-dev --no-emit-project \
    --project /tmp/src --output-file /tmp/src/requirements.txt \
  && pip install --no-cache-dir --require-hashes --only-binary :all: \
    --no-binary pymongo --no-deps -r /tmp/src/requirements.txt \
  && rm -rf /tmp/src

## Copy the app, which runs from its source directory
COPY --chown=1005:1005 ./app /app/app
RUN chmod 777 /app/app/api/

USER 1005

CMD ["bash", "-c", "cd /app/app; python app.py"]
