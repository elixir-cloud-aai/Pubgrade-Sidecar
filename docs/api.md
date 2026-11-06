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

Notify the sidecar of a new image tag for a deployment:

```bash
curl --location --request PUT 'https://{sidecar-host}/deployment/{deployment-name}' \
  --header 'X-Access-Token: {access-token}' \
  --header 'Content-Type: application/json' \
  --data-raw '{"image_name": "elixircloud/tesk-api", "tag": "1.2.0"}'
```

A notification of a new image returns at once, with status 202; the rollout
runs in the background. If the deployment already uses the image, it returns
200 and nothing happens. Only the tag can change, and `docker.io/<repository>` and
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

`status` is `waiting_for_image`, `rolling_out`, `complete` or `failed` (with
an explanation in `detail`). A failure counts until the deployment has the
requested image, and a change of the deployment after a rollout, e.g., a
rollback, counts over the last notification. A newer notification replaces
one that still waits for its image.

`requested_image` and `detail` are kept in memory: a restart loses a notification
that is still waiting for its image, without a report. Afterwards, `status` only
tells the state of the deployment, so compare `image` with what was notified.
