# Development

CI (`.github/workflows/ci.yml`) checks the pull request title, runs the linters
and type checks, validates the citation metadata (`CITATION.cff`), runs the
unit tests with their coverage, builds and starts the image, scans it for
known vulnerabilities, lints and tests the chart, runs the integration test
and builds the documentation.

Dependencies are declared in `pyproject.toml` and locked, with their hashes, in
`uv.lock`; the image installs exactly the locked runtime dependencies. Use
[uv](https://docs.astral.sh/uv/) (pip 24.1 or newer rejects the metadata of a
dependency of FOCA 0.12) and Python 3.9, the version of the base image. To set
up the environment, with the dev dependencies, and run the unit tests:

```bash
uv sync --python 3.9
uv run pytest --cov
```

They need neither a cluster nor a Kubernetes configuration, and fail below 100%
coverage. The chart tests in `test/chart` run if Helm is installed. After
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
test/integration/k3s.sh
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
by hand.

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
