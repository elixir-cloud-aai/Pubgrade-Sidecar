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

CI publishes [`elixircloud/pubgrade-sidecar`](https://hub.docker.com/r/elixircloud/pubgrade-sidecar)
on every push to `dev`, after all checks have passed, with three tags:
the date (`YYYYMMDD` in UTC, moved by each push of that day), `sha-<commit>`,
and `latest`. The chart defaults to `latest`; for a deployment, set `image.tag`
to `sha-<commit>`, the only tag that stays on one commit (only a re-run of CI
for that commit rebuilds it).

Publishing runs in the environment `dev-images` and needs the Docker Hub
secrets `DOCKERHUB_LOGIN` and `DOCKERHUB_TOKEN` (organization secrets); the
environment allows only the branch `dev`. A fork publishes to its own Docker
Hub organization once it sets these secrets and the Actions variable
`DOCKERHUB_ORG` (for the repository or organization, not the environment);
otherwise it skips publishing. To deploy a fork's image, set `image.repository` to
`<organization>/pubgrade-sidecar`.
