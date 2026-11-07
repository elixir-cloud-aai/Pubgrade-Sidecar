# Development

CI (`.github/workflows/ci.yml`) checks the pull request title, runs the linters
and type checks, validates the citation metadata (`CITATION.cff`), runs the
unit tests with their coverage, builds and starts the image, scans it for
known vulnerabilities, lints and tests the chart, runs the integration test
and builds the documentation.

Dependencies are declared in `pyproject.toml` and locked, with their hashes, in
`uv.lock`; the image installs exactly the locked runtime dependencies. Use
[uv](https://docs.astral.sh/uv/) and Python 3.13, the version of the base image
(`requires-python` in `pyproject.toml`). To set up the environment, with the
dev dependencies, and run the unit tests:

```bash
uv sync --python 3.13
uv run pytest --cov
```

The app is built on [FOCA](https://foca.readthedocs.io/): its configuration is
`app/config/config.yaml`, and `app/pubgrade_sidecar/app.py` creates the app
(`create_app()`). The image serves it with Gunicorn, with FOCA's settings
(`foca.gunicorn_config`): one worker process with threads, as the sidecar
keeps its deliveries in memory. To run it locally with Flask's development
server, on port 8080:

```bash
cd app
ACCESS_TOKEN=local uv run python -m pubgrade_sidecar.app
```

They need neither a cluster nor a Kubernetes configuration, and fail below 100%
coverage. The chart tests in `tests/chart` run if Helm is installed. The test
of the availability check against a real registry runs if `OCI_REGISTRY` names
one that serves HTTP, as in CI:

```bash
docker run --detach --rm --name registry --publish 5000:5000 registry:2
OCI_REGISTRY=localhost:5000 uv run pytest tests/unit/test_registry.py
docker stop registry
```

It checks anonymous access only; token and Basic authentication are tested
with mocked responses. After
changing dependencies in `pyproject.toml`, update the lock with `uv lock`; CI
fails if it doesn't match.

The integration test runs the sidecar on a real, throw-away cluster: it builds
the image, starts k3s in a Docker container, installs the chart into two
namespaces and checks that notifications roll the target Deployments out to
the new tag and that the status reports it, with the chart's minimal Role, and
in one namespace under an arbitrary user ID with group 0, as on OpenShift. It
takes about five minutes and needs bash 4.4 or newer, Docker (privileged
containers), kubectl, Helm, curl, jq and access to Docker Hub. At the end, it
removes the cluster, with its volumes, and the image it built; kubectl and Helm
use a kubeconfig of their own, so other clusters aren't touched:

```bash
tests/integration/k3s.sh
```

`K3S_IMAGE` sets the k3s image, and `API_PORT` and `FORWARD_PORT` the local
ports (16443, and 18080 and 18081). `KEEP=1` keeps the cluster and the image
for debugging; remove them afterwards with
`docker rm -f -v pubgrade-sidecar-integration` and
`docker rmi pubgrade-sidecar:integration`.

Linters (ruff), type checks (mypy) and the secret scan (gitleaks) run with
[pre-commit](https://pre-commit.com/), on every commit once installed, and in
CI:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

The mypy hook runs the mypy of the environment, so activate it
(`source .venv/bin/activate`) before committing.

Dependabot proposes updates of the locked dependencies, the actions, the image
of uv and the hooks. FOCA, and with it connexion and the base image, is updated
by hand: the tag and digest of the base image in the `Dockerfile` (its FOCA and
Python versions), `foca` in `pyproject.toml` and the lock (`uv lock`) together,
following FOCA's migration guides. The unit tests
(`tests/unit/test_image.py`) fail if the base image's FOCA isn't the locked
one, or its Python isn't the one `pyproject.toml` requires.

The documentation is built with [MkDocs](https://www.mkdocs.org/) and
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) from
`docs/` (`mkdocs.yml`), with the dependency group `docs` of the lock; the code
reference comes from the docstrings. It is published as the Sidecar section
of [Pubgrade's documentation](https://pubgrade.readthedocs.io/en/latest/sidecar/),
whose build pulls in `docs/` from this repository; this repository publishes
nothing itself. So keep the pages self-contained: link to other pages of
`docs/` with relative links, to files of the repository with absolute links
(on GitHub), and don't rely on the navigation of the site. The README only
points to the documentation. Serve it locally while editing, or build it as
CI does, which fails on warnings, e.g., broken links:

```bash
uv run --group docs mkdocs serve
uv run --group docs mkdocs build --strict
```

## Versioning and releases

The app and its chart are released together, with one version: the tag
`vX.Y.Z` (or `vX.Y.ZaN`, `vX.Y.ZbN` and `vX.Y.ZrcN` for pre-releases), the
version in `pyproject.toml`, the chart's `appVersion` (the default image
tag) and `version` (the SemVer form, e.g., `1.2.3-rc1` for `1.2.3rc1`), and
the version in `CITATION.cff`. Until 1.0.0, all releases are GitHub
pre-releases.

For the first release, the chart's `version` (0.2.0, from before the
release workflow) and the app's version (0.1.0) differ; the release-prep pull
request sets both, e.g., to 0.2.0.

1. Release-prep pull request to `dev`: set the version in `pyproject.toml`,
   `deployment/Chart.yaml` (`version` and `appVersion`) and `CITATION.cff`
   (`python scripts/citation.py set <version>`, which also sets the release
   date), and run `uv lock`.
2. Once CI has passed for its commit on `dev`, bring that commit to `main`.
3. Tag it on `main` with a signed, annotated tag and push the tag:
   `git tag -s v<version> -m v<version>`, then
   `git push origin v<version>`.

The release workflow (`.github/workflows/release.yml`) checks the tag (a
release version, annotated, with a signature that GitHub verified, on
`main`), that the versions above match it (`scripts/check_versions.py` and
`scripts/citation.py check`) and that CI passed for a push of the commit. It
packages the chart, pushes the image by digest, scans it, then attests and
signs the image and the chart, tags the image `X.Y.Z` (and `latest` if it is
the highest stable release; `scripts/release_version.py`) and creates the
GitHub release, with the chart and its Sigstore bundle attached. Pushing the
image and publishing the release each need approval in the environment
`release`.

The release gets GitHub's generated notes; for a first release (no published
release before it), `scripts/release_notes.py` builds them instead, because
without a previous tag GitHub starts at an older tag of its own choice. Edit
the notes by hand into the agreed format after publishing: the notes of
immutable releases stay editable, their assets don't. So the release is
created as a draft, gets its assets and is published last
(`scripts/release_assets.py`); a re-run of failed jobs finishes it. Don't
re-run all jobs of a release that was published: that builds a new image
digest and moves the release's image tags to it before it fails at the
release.

The images are scanned for known vulnerabilities with Trivy: in CI, in the
release workflow before the image is signed and tagged, and weekly for
`latest` (`.github/workflows/scan.yml`). The scans in CI and of releases fail
on critical and high vulnerabilities that have a fix; accept a finding only
with a reason and an expiry date, in `.trivyignore.yaml`. The weekly scan
doesn't fail until the first release, as `latest` is still the last image
built on FOCA 0.7; once the first release has published `latest`, drop
`fail: "false"` there so that it fails too.

The scripts in `scripts/` use only the standard library and are tested in
`tests/unit/`, with the app's coverage.
