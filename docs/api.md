# API

The sidecar's API is described by its OpenAPI specification,
[`app/api/pubgrade_sidecar.yaml`](https://github.com/elixir-cloud-aai/Pubgrade-Sidecar/blob/dev/app/api/pubgrade_sidecar.yaml);
a running sidecar serves it with a Swagger UI at `/ui/`.

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

Notify the sidecar of a new image tag for all deployments that use its
repository (since version 3.1.0 of the API):

```bash
curl --location --request PUT 'https://{sidecar-host}/deployment' \
  --header 'X-Access-Token: {access-token}' \
  --header 'Content-Type: application/json' \
  --data-raw '{"image_name": "elixircloud/tesk-api", "tag": "1.2.0"}'
```

This is the callback URL to subscribe in Pubgrade for a service with several
images, or several deployments of one image: subscribe it once per image
repository. In every deployment the sidecar manages (`targetDeployments` of
the chart; with `deploymentLabel`, only those with that label, see
[Deployment](deployment.md)), every container whose image repository equals
`image_name` gets the new tag. Other containers stay as they are. With 202,
the response lists the deployments that roll out, with the requested image of
their first container of the repository (with 200, all deployments that use
it, with the image they use):

```json
{
  "message": "Updating images in progress...",
  "deployments": [
    {"name": "tesk-api", "image": "elixircloud/tesk-api:1.2.0"},
    {"name": "tesk-worker", "image": "elixircloud/tesk-api:1.2.0"}
  ]
}
```

Each deployment gets its own delivery, whose state `GET
/deployment/{deployment-name}` reports. If no container of these deployments
uses the repository, the call gets a 404; a listed deployment that doesn't
exist is skipped.

Notify the sidecar of a new image tag for one deployment:

```bash
curl --location --request PUT 'https://{sidecar-host}/deployment/{deployment-name}' \
  --header 'X-Access-Token: {access-token}' \
  --header 'Content-Type: application/json' \
  --data-raw '{"image_name": "elixircloud/tesk-api", "tag": "1.2.0"}'
```

Every container of the deployment whose image repository equals `image_name`
gets the new tag; if none does, the call gets a 409 (see
[Upgrading to version 3.1.0 of the API](deployment.md#upgrading-to-version-310-of-the-api)).

A notification of a new image returns at once, with status 202; the rollout runs
in the background. If the deployments already use the image, it returns 200 and
nothing happens. Only the tag can change, and `docker.io/<repository>` and
`<repository>` count as the same repository. The sidecar waits for the image in
its registry, about two minutes (longer if the registry answers slowly): Docker
Hub for repositories without a registry host, otherwise the registry named in
the repository, e.g., `harbor.example.org/project/tesk-api`. Any registry that
implements the OCI distribution API should work, e.g., Harbor, GitLab, Quay or
GHCR (CI tests the sidecar with `registry:2`); for private images, the sidecar
needs credentials (see [Deployment](deployment.md#registries)). Use a new tag
for every release: a tag that the deployment already uses, like one pushed
again, doesn't lead to a rollout.

```text
Status  Meaning
200     The deployment already uses this image; nothing happens
202     Accepted
400     Invalid request, e.g., no `X-Access-Token` header or an invalid tag
401     Wrong token, or no token configured in the sidecar
403     The sidecar may not access this deployment
404     Deployment not found; for `PUT /deployment`: no container uses the
        repository
405     Method not allowed
409     No container of the deployment uses the image repository; nothing
        changed
413     Request body larger than 64 KiB
415     Media type not supported
500     The Kubernetes API failed; try again
```

Since version 3.0.0 of the API, errors are
[problem details](https://www.rfc-editor.org/rfc/rfc9457)
(`application/problem+json`), with `title`, `status` (an integer) and `detail`,
e.g.:

```json
{
  "title": "Conflict",
  "status": 409,
  "detail": "The deployment uses another image repository."
}
```

For invalid requests, `detail` tells what is wrong, e.g.,
`Missing header parameter 'X-Access-Token'`. Before, errors had `msg` and
`status_code` (a string) instead (see
[Upgrading to version 3.0.0 of the API](deployment.md#upgrading-to-version-300-of-the-api)).

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

`image` is the image of the deployment's first container. `status` is
`waiting_for_image`, `rolling_out`, `complete` or `failed` (with
an explanation in `detail`). A failure counts until the deployment has the
requested image, and a change of the deployment after a rollout, e.g., a
rollback, counts over the last notification. A newer notification replaces
one that still waits for its image.
A deployment with containers of several image repositories keeps the last
notification per repository, so that they don't replace each other. A failed
delivery of any of them, then a waiting one, counts over the deployment's own
status, and `requested_image` names its image; otherwise `requested_image` is
the image of the last notification. Right before its patch, a delivery
reads the deployment again: containers added, removed or reordered meanwhile
count, and a deployment that no longer uses the image repository fails the
delivery, without a change. Containers pinned by a digest lose it when they
get a new tag.

`requested_image` and `detail` are kept in memory: a restart loses a notification
that is still waiting for its image, without a report. Afterwards, `status` only
tells the state of the deployment, so compare `image` with what was notified.
