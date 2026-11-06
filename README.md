# Pubgrade-Sidecar

<!-- --8<-- [start:synopsis] -->
A microservice which would be co-deployed alongside
[Pubgrade](https://github.com/elixir-cloud-aai/Pubgrade) to listen for update
notifications from Pubgrade and update the subscribed service accordingly. The
microservice is named as sidecar as it would be co-deployed with the service of
interest (service registered for updates at Pubgrade) in cloud native
infrastructure at deployment side.

> **_NOTE:_**  Project is still under development.
<!-- --8<-- [end:synopsis] -->

The documentation (operator guide, API, development notes and code reference)
is in [`docs/`](docs/index.md).

## Image

<!-- --8<-- [start:image] -->
CI publishes [`elixircloud/pubgrade-sidecar`](https://hub.docker.com/r/elixircloud/pubgrade-sidecar)
on every push to `master`, after all checks have passed, with three tags:
the date (`YYYYMMDD` in UTC, moved by each push of that day), `sha-<commit>`,
and `latest`. The chart defaults to `latest`; for a deployment, set `image.tag`
to `sha-<commit>`, the only tag that stays on one commit (only a re-run of CI
for that commit rebuilds it).

Publishing runs in the environment `dev-images` and needs the Docker Hub
secrets `DOCKERHUB_LOGIN` and `DOCKERHUB_TOKEN`; the environment should allow
only the branch `master`. A fork publishes to its own Docker Hub organization
once it sets these secrets and the Actions variable `DOCKERHUB_ORG` (for the
repository or organization, not the environment); otherwise it skips
publishing. To deploy a fork's image, set `image.repository` to
`<organization>/pubgrade-sidecar`.
<!-- --8<-- [end:image] -->

## Installation

<!-- --8<-- [start:installation] -->

The chart in `deployment/` installs the sidecar into the namespace of the
Deployments to upgrade, on Kubernetes and on OpenShift. It needs no fixed user
ID, so OpenShift's default security constraints apply.

1. Create a Secret with a token of its own for this site:

   ```bash
   kubectl -n production create secret generic pubgrade-sidecar \
     --from-literal=access-token="$(openssl rand -hex 32)"
   ```

2. Write the values, e.g., `my-values.yaml`:

   ```yaml
   targetDeployments:
     - tesk-api
   accessToken:
     existingSecret: pubgrade-sidecar
   hostName: sidecar.example.org
   # On OpenShift:
   route:
     enabled: true
   # Elsewhere, instead:
   # ingress:
   #   enabled: true
   #   className: nginx
   #   tlsSecretName: sidecar-tls
   ```

   Without a Route or Ingress, the sidecar is only reachable in the cluster.
   A Route redirects HTTP to HTTPS. With an Ingress, that depends on the
   controller (ingress-nginx does it by default if TLS is set): check it, and
   set `ingress.annotations` if needed, as every call carries the token.

3. Install it:

   ```bash
   helm install pubgrade-sidecar deployment/ -n production -f my-values.yaml
   ```

The sidecar gets `get` and `patch` on the `targetDeployments` and nothing else.
So with the chart, `GET /deployment` and `DELETE /deployment/{name}` are refused:
they would need further rights, which you can add with a Role of your own.
Kubernetes can't limit `patch` to one field, so these rights cover the whole of
each listed Deployment, although the sidecar's API only changes the image tag.
List only what the sidecar has to update, and add an admission policy in your
cluster if you need a stricter limit.
All values are described in `deployment/values.yaml`.

To change the token, update the Secret and restart the sidecar, which reads the
token at start:

```bash
kubectl -n production rollout restart deployment/pubgrade-sidecar
```

### Upgrading from chart version 0.1

Uninstall the old release first (`helm uninstall`), then install as above:
Kubernetes doesn't allow changing the role of an existing RoleBinding, so
`helm upgrade` fails. The values changed:

```text
Old                  New
appname              appName
imagename            image.repository, image.tag
serviceaccountname   appName
containerport        service.port
secret               accessToken.existingSecret, a Secret that you create
clusterType          route.enabled or ingress.enabled
nodeport             none
none                 targetDeployments, required
```
<!-- --8<-- [end:installation] -->

## Usage

<!-- --8<-- [start:usage] -->
All calls need the access token, which the sidecar takes from the environment
variable `ACCESS_TOKEN`. Without that variable, it rejects all calls.

The examples call the sidecar through its Route or Ingress, with TLS
(`https://{sidecar-host}`, the `hostName` of the chart). Within the cluster,
without a Route or Ingress, call its Service over plain HTTP instead, e.g.,
`http://pubgrade-sidecar.{namespace}:8080`.

Get the deployments in the namespace:

```bash
curl --location --request GET 'https://{sidecar-host}/deployment' \
  --header 'X-Access-Token: {access-token}'
```

Get the image and the rollout status of a deployment:

```bash
curl --location --request GET 'https://{sidecar-host}/deployment/{deployment-name}' \
  --header 'X-Access-Token: {access-token}'
```

Notify the sidecar of a new image tag for a deployment:

```bash
curl --location --request PUT 'https://{sidecar-host}/deployment/{deployment-name}' \
  --header 'X-Access-Token: {access-token}' \
  --header 'Content-Type: application/json' \
  --data-raw '{"image_name": "elixircloud/tesk-api", "tag": "1.2.0"}'
```

A notification returns at once, with status 202; the rollout runs in the
background. Only the tag can change, and `docker.io/<repository>` and
`<repository>` count as the same repository. The sidecar waits for the image
on Docker Hub, about two minutes at most, so only images hosted there can be
rolled out. Use a new tag for every release: a tag that the deployment already
uses, like one pushed again, doesn't lead to a rollout.

```text
Status  Meaning
200     The deployment already uses this image; nothing happens
202     Accepted
400     Invalid request, e.g., no `X-Access-Token` header or an invalid tag
401     Wrong token, or no token configured in the sidecar
403     The sidecar may not access this deployment
404     Deployment not found
405     Method not allowed
409     Another image repository, or one not on Docker Hub; nothing changed
415     Media type not supported
500     The Kubernetes API failed; try again
```

The `GET` call above reports the state of the delivery. Since version 2.0.0 of
the API, it returns this object instead of only the image:

```json
{
  "image": "elixircloud/tesk-api:1.2.0",
  "status": "complete",
  "replicas": 1,
  "updated_replicas": 1,
  "available_replicas": 1,
  "requested_image": "elixircloud/tesk-api:1.2.0"
}
```

`status` is `waiting_for_image`, `rolling_out`, `complete` or `failed` (with
an explanation in `detail`). A failure counts until the deployment has the
requested image, and a change of the deployment after a rollout, e.g., a
rollback, counts over the last notification. A newer notification replaces
one that still waits for its image.

`requested_image` and `detail` are kept in memory: a restart loses a notification
that is still waiting for its image, without a report. Afterwards, `status` only
tells the state of the deployment, so compare `image` with what was notified.
<!-- --8<-- [end:usage] -->

## Development

<!-- --8<-- [start:development] -->
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
reference comes from the docstrings. Its pages include the sections of this
README, between the `--8<--` markers, so that nothing is written twice. Serve
it locally while editing, or build it as CI and Read the Docs
(`.readthedocs.yaml`) do, which fails on warnings, e.g., broken links:

```bash
uv run --group docs mkdocs serve
uv run --group docs mkdocs build --strict
```
<!-- --8<-- [end:development] -->

## Citation

If you use Pubgrade-Sidecar, please cite it with the metadata in
[`CITATION.cff`](CITATION.cff).

## License

[Apache License 2.0](LICENSE)
