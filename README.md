# Pubgrade-Sidecar

A microservice which would be co-deployed alongside [pubgrade][pubgrade] to
listen for update notifications from [pubgrade][pubgrade] and update the
subscribed service accordingly. The microservice is named as sidecar as it
would be co-deployed with the service of interest (service registered for
updates at pubgrade) in cloud native infrastructure at deployment side.

> **_NOTE:_**  Project is still under development.

## Installation

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

## Usage

All calls need the access token, which the sidecar takes from the environment
variable `ACCESS_TOKEN`. Without that variable, it rejects all calls.

- Get available deployments in the namespace
 ```bash
 curl --location --request GET 'http://{pubgrade_sidecar-url}/deployment' \
 --header 'X-Access-Token: {access-token}'
 ```

- Get the image and the rollout status of the deployment.
```bash
curl --location --request GET 'http://{pubgrade_sidecar-url}/deployment/
{deployment-name}' \
--header 'X-Access-Token: {access-token}'
```

- Modify image tag of the deployment.
```bash
curl --location --request PUT 'http://{pubgrade_sidecar-url}/deployment/
{deployment-name}' \
--header 'X-Access-Token: {access-token}' \
--header 'Content-Type: application/json' \
--data-raw '{
    "image_name": "elixircloud/tesk-api",
    "tag": "1.2.0"
}'
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
400     Invalid request, e.g., a missing token or an invalid tag
401     Wrong token
403     The sidecar may not access this deployment
404     Deployment not found
409     Another image repository, or one not on Docker Hub; nothing changed
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
rollback, counts over the last notification. A newer notification replaces one that still waits for its
image.

`requested_image` and `detail` are kept in memory: a restart loses a notification
that is still waiting for its image, without a report. Afterwards, `status` only
tells the state of the deployment, so compare `image` with what was notified.

## Development

CI (`.github/workflows/ci.yml`) checks the pull request title, runs the linters
and type checks, runs the unit tests with their coverage, builds and starts the
image, scans it for known vulnerabilities and lints and tests the chart.

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

Linters (ruff), type checks (mypy) and the secret scan (gitleaks) run with
[pre-commit](https://pre-commit.com/), on every commit once installed, and in
CI:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

Dependabot proposes updates of the locked dependencies, the actions, the image
of uv and the hooks. FOCA, and with it connexion and the base image, is updated
by hand.

## License

[Apache License 2.0](LICENSE)

[pubgrade]: <https://github.com/elixir-cloud-aai/Pubgrade>
