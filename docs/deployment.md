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
   (see [Image](#image)). Until the first release, that version has no image
   yet: set `image.tag` to `sha-<commit>` or `dev`.

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

## Changed image tags (November 2026)

Until the release workflow was added (November 2026), every push to `dev`
moved the image tag `latest` (and a dated tag, `YYYYMMDD`), and the chart
deployed `latest` by default. Now pushes to `dev` publish `dev` and
`sha-<commit>`, `latest` moves only with releases, to the highest stable
release, and dated tags are no longer published (existing ones stay as they
are). The chart deploys the image of its `appVersion` unless `image.tag` is
set. So `latest` stays at the last image published from `dev` before the
change until the first release. If you pull `latest` (or deployed the chart
with its default tag) to follow development, switch to `sha-<commit>` (or
`dev`), or to a release once there is one; until the first release, set
`image.tag` when you install the chart (see above).

## Upgrading to version 3.0.0 of the API

Version 3.0.0 of the API (November 2026) runs on FOCA 0.21 and Python 3.13
instead of FOCA 0.7 and Python 3.9. Calls and their successful responses stay
the same, and so do the chart and its values. What changes:

- **Errors are problem details** (`application/problem+json`, see
  [API](api.md)): `title`, `status` (an integer) and `detail` instead of `msg`
  and `status_code` (a string). Clients that read the error body need to
  change; Pubgrade only reads the status code.
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
