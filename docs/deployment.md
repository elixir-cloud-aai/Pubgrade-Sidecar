# Deployment

The chart in
[`deployment/`](https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/tree/dev/deployment)
installs the sidecar into the namespace of the Deployments to upgrade, on
Kubernetes and on OpenShift. It needs no fixed user
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

## Image tags since the first release

Until the first release, every push to `dev` moved the image tag `latest`
(and a dated tag, `YYYYMMDD`), and the chart deployed `latest` by default.
Since then, `latest` moves only with releases, to the highest stable release,
pushes to `dev` publish `dev` and `sha-<commit>` instead, and the dated tags
are no longer published (existing ones stay as they are). The chart deploys
the image of its `appVersion` unless `image.tag` is set. If you pull `latest`
(or deployed the chart with its default tag) to follow development, switch
to `dev`, or better to `sha-<commit>`, or to a release. Before the first
release, the chart's `appVersion` names a version that has no image yet: set
`image.tag` until then.

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
have passed, in the environment `dev-images`. This needs the Docker Hub
secrets `DOCKERHUB_LOGIN` and `DOCKERHUB_TOKEN` (organization secrets); the
environment allows only the branch `dev`. Releases are published from version
tags, in the environment `release` (see
[Versioning and releases](development.md#versioning-and-releases)). A fork publishes to its own Docker
Hub organization once it sets these secrets and the Actions variable
`DOCKERHUB_ORG` (for the repository or organization, not the environment);
otherwise it skips publishing. To deploy a fork's image, set
`image.repository` to `<organization>/pubgrade-sidecar`.
