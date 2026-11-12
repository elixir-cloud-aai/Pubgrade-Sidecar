# Deployment

The chart in
[`deployment/`](https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/tree/dev/deployment)
installs the sidecar into the namespace of the Deployments to upgrade, on
Kubernetes and on OpenShift. It needs no fixed user ID, so OpenShift's
default security constraints apply.

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

   The chart deploys the image of its `appVersion` unless `image.tag` is set
   (see [Image](#image)): on `dev` and in a release, the image of that
   release (e.g., `0.2.0`). To follow development instead, set `image.tag` to
   `sha-<commit>` or `dev`.

`PUT /deployment` updates all `targetDeployments` that use the notified image
repository (see [API](api.md)). To limit it to some of them, set
`deploymentLabel` to a label they have, `key=value`, or `key` for any value (a
value without a key, e.g., `=api`, matches no deployment); `GET` and
`PUT /deployment/{name}` aren't limited by it. Outside the chart, the sidecar
takes these settings from the environment variables `TARGET_DEPLOYMENTS`
(comma-separated names; without it, all deployments in the namespace, which
needs the right to list them) and `DEPLOYMENT_LABEL`. `GET /deployment` lists
the same deployments as `PUT /deployment` updates, and the calls for one
deployment (`GET` and `PUT /deployment/{name}`) answer 403 for a deployment
that `TARGET_DEPLOYMENTS` doesn't name. The deployments of
`TARGET_DEPLOYMENTS` are read one after the other (each within 10 seconds),
so with many of them, or a slow Kubernetes API, `GET /deployment` and
`PUT /deployment` take longer than a caller such as Pubgrade (10 seconds by
default) may wait: keep the list to what the sidecar has to update.

The sidecar gets `get` and `patch` on the `targetDeployments` and nothing else.
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

## Checking a deployed sidecar

[`tests/check_deployment.sh`](https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/blob/dev/tests/check_deployment.sh)
checks a sidecar from outside, over its Route or Ingress, after installing or
upgrading it, or after changing the token. It only reads: it sends no
notification, so nothing is rolled out. It needs bash, curl and jq, and checks
that:

- the URL is `https://` (otherwise it stops before sending the token), the
  certificate is valid, and plain HTTP on port 80 of the same host doesn't
  serve the API (skipped if the URL has a port). It passes on no answer
  (connection refused or timed out; other errors, e.g., of name resolution,
  fail), a redirect to HTTPS, or an error that the API doesn't give a call
  without a token (anything but 400 and 401, e.g., 404 from a default
  backend); 2xx and other redirects fail;
- calls without a token get 400, and calls with a wrong one 401;
- `GET /deployment` lists the target Deployment (with `deploymentLabel`, it
  must have that label), and `GET /deployment/{name}` reports its status
  (printed);
- a Deployment that isn't in `targetDeployments` gets 403 (by default
  `pubgrade-check-not-a-target`, which needn't exist; a third argument names
  another one).

The token comes from the environment variable `ACCESS_TOKEN` and goes to curl
on its standard input, never on the command line. For example, with the token
from the Secret of step 1:

```bash
ACCESS_TOKEN="$(kubectl -n production get secret pubgrade-sidecar \
  -o jsonpath='{.data.access-token}' | base64 -d)" \
  tests/check_deployment.sh https://sidecar.example.org tesk-api
```

It prints one line per check (`ok`, `FAIL` or `skip`) and exits with 0 if all
passed, 1 if any failed, and 2 for wrong usage; a URL that isn't `https://`
fails (1) before any call. Calls over HTTPS use the proxy of the environment
(`https_proxy`), and the check of plain HTTP that of `http_proxy`: behind a
proxy, that check sees the proxy's answer. `CA_CERT` names a CA certificate
to verify the server with, e.g., for a private CA. `CHECK_ALLOW_HTTP=1`
allows an `http://` URL to `localhost`, `127.0.0.1` or `[::1]` (e.g., a
`kubectl port-forward`), never through a proxy, and skips the HTTPS checks;
the integration test uses it.

## Registries

Before a rollout, the sidecar checks that the image is in its registry, with
the OCI distribution API: Docker Hub for repositories without a registry host,
otherwise the registry named in the repository (e.g., a Harbor project,
`harbor.example.org/project/tesk-api`). It asks anonymously over HTTPS, which
suffices for public images. For private images, create a Secret with
credentials that may pull them (e.g., a Harbor robot account), and name it in
`registryCredentials.existingSecret`:

```bash
kubectl -n production create secret docker-registry registry-credentials \
  --docker-server=harbor.example.org \
  --docker-username='robot$sidecar' --docker-password=<secret>
```

The chart mounts its key `.dockerconfigjson` (set another one with
`registryCredentials.key`, e.g., for a Docker `config.json`) and passes the
path in `REGISTRY_AUTH_FILE`. Only its `auths` entries are read, with `auth` or
`username` and `password` (not credential helpers or identity tokens). If the
registry names a token service, as Docker Hub and Harbor do, the credentials go
to that service, as with Docker's own client, but only over HTTPS (or to an
insecure registry itself); token services over HTTP aren't used otherwise, and
neither are challenges after a redirect to another host or from HTTPS to HTTP.
The sidecar only uses the credentials to check images; the cluster pulls images with
the Deployments' own `imagePullSecrets`. Registries that serve only HTTP, e.g.,
one in the cluster, go in `insecureRegistries` (`host` or `host:port`;
environment variable `INSECURE_REGISTRIES`, comma-separated); credentials for
them are sent unencrypted.

## Changed image tags (November 2026)

Until the release workflow was added (November 2026), every push to `dev`
moved the image tag `latest` (and a dated tag, `YYYYMMDD`), and the chart
deployed `latest` by default. Now pushes to `dev` publish `dev` and
`sha-<commit>`, `latest` moves only with releases, to the highest stable
release, and dated tags are no longer published (existing ones stay as they
are). The chart deploys the image of its `appVersion` unless `image.tag` is
set. Since the first release, 0.2.0, `latest` is the image of the highest
stable release. If you pulled `latest` (or deployed the chart with its
default tag) to follow development, switch to `sha-<commit>` (or `dev`), or
pin a release.

## Upgrading to version 4.0.0 of the API

Version 4.0.0 of the API removes `DELETE /deployment/{deployment-name}`,
which deleted the pod of that name (not a deployment) and which Pubgrade
never called; it now gets a 405. With the chart, its Role refused the call
already, so deployments with the chart change nothing. If you gave the
sidecar the right to delete pods with a Role of your own for this call,
remove that right. Calls that update deployments (`PUT`) and read them
(`GET`) are unchanged.

## Upgrading to version 3.2.0 of the API

Version 3.2.0 of the API applies `TARGET_DEPLOYMENTS` (the chart's
`targetDeployments`) to `GET /deployment` and to `GET` and
`PUT /deployment/{deployment-name}`, not only to `PUT /deployment`. These
calls change as follows:

- `GET /deployment` lists the deployments that `PUT /deployment` updates:
  those in `TARGET_DEPLOYMENTS` that exist (read one by one, so the chart's
  Role suffices), or all in the namespace without it; with
  `DEPLOYMENT_LABEL`, only those with that label. Before, it listed all
  deployments in the namespace, which the chart's Role refused.
- `GET` and `PUT /deployment/{deployment-name}` answer 403 for a deployment
  that `TARGET_DEPLOYMENTS` doesn't name, without calling the Kubernetes
  API. With the chart, its Role refused these calls already.
- Notifications whose `image_name` has a tag or a digest (e.g.,
  `org/app:1.0`) get a 400. Before, the sidecar dropped them and answered
  202, and the delivery failed later. A registry's port (e.g.,
  `registry:5000/org/app`) is no tag.
- `registry-1.docker.io/` in `image_name` counts as Docker Hub, as
  `docker.io/` and `index.docker.io/` do.

## Upgrading to version 3.1.0 of the API

Version 3.1.0 of the API adds `PUT /deployment`, for all deployments that use
an image repository (see [API](api.md)), and the chart value
`deploymentLabel`. Existing calls stay, with one change:
`PUT /deployment/{deployment-name}` updates every container of the
deployment whose image repository equals `image_name`. Before, it updated
only the first container, and answered 409 if that one used another
repository; now a 409 means that no container uses it.

Images are no longer limited to Docker Hub: the sidecar checks for them in the
registry named in the repository, with optional credentials and the chart
values `registryCredentials` and `insecureRegistries` (see
[Registries](#registries)). Repositories on other registries, which got a 409
before, are now rolled out.

## Upgrading to version 3.0.0 of the API

Version 3.0.0 of the API (November 2026) runs on FOCA 0.22 and Python 3.13
instead of FOCA 0.7 and Python 3.9. Calls and their successful responses stay
the same, and so do the chart and its values. What changes:

- **Errors are problem details** (`application/problem+json`, see
  [API](api.md)): `title`, `status` (an integer) and `detail` instead of `msg`
  and `status_code` (a string), and for requests that don't match the API's
  schemas, the invalid fields in `errors`. Clients that read the error body
  need to change; Pubgrade only reads the status code.
- Errors of the Kubernetes API other than `403` and `404` are answered with
  `500`, as documented, instead of passing on their status (e.g., `503`).
- Request bodies larger than 64 KiB are rejected with `413`.
- The image serves the app with Gunicorn instead of Flask's development
  server, still on port 8080, with one worker process (keep it at one: the
  sidecar keeps its deliveries in memory). Gunicorn's settings can be changed
  via `GUNICORN_CMD_ARGS`.
- The image runs as user 1000 (group 0), the user of the FOCA image, instead
  of 1005, and its app files are read-only. The chart sets no fixed user, so
  it needs no change; deployments of their own that set `runAsUser` may set
  any user ID.

## Upgrading from chart version 0.1

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

## Image

The image is published as
[`elixircloud/pubgrade-sidecar`](https://hub.docker.com/r/elixircloud/pubgrade-sidecar),
with these tags:

```text
Tag              Image                                     Moves
X.Y.Z            release X.Y.Z (also X.Y.ZrcN etc.)        no
latest           highest stable release                    with each release
dev              newest commit on branch dev               with each push to dev
sha-<commit>     that commit on branch dev                 no
```

Releases are signed (Sigstore) and come with build provenance attestations;
each GitHub release also has the chart of that version attached. The chart
deploys the image of its `appVersion`, i.e., of its release, unless you set
`image.tag`. To try unreleased changes, set `image.tag` to `sha-<commit>`
(the only tag of a `dev` image that stays on one commit) or `dev`.

Images from `dev` are published by CI on every push to `dev`, after all checks
have passed, in the environment `dev-images`, which allows only the branch
`dev`. Releases are published from version tags, in the environment
`release` (see
[Versioning and releases](development.md#versioning-and-releases)). Both
need the Docker Hub secrets `DOCKERHUB_LOGIN` and `DOCKERHUB_TOKEN`
(organization secrets).

A fork publishes to its own Docker Hub organization once it sets these
secrets and the Actions variable `DOCKERHUB_ORG` (for the repository or
organization, not the environment); otherwise it skips publishing. To deploy
a fork's image, set `image.repository` to `<organization>/pubgrade-sidecar`.
