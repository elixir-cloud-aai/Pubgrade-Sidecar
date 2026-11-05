# Pubgrade-Sidecar

A microservice which would be co-deployed alongside [pubgrade][pubgrade] to 
listen for update notifications from [pubgrade][pubgrade] and update the 
subscribed service accordingly. The microservice is named as sidecar as it 
would be co-deployed with the service of interest (service registered for 
updates at pubgrade) in cloud native infrastructure at deployment side.

> **_NOTE:_**  Project is still under development.

## Installation

1. Install and setup Kubernetes.
2. Install Helm.
3. Modify deployment/values.yaml.
4. Create a namespace, e.g., with kubectl create namespace production
5. Create a deployment using Helm with 
  `helm install pubgrade-sidecar deployment/ -n production`

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

CI (`.github/workflows/ci.yml`) checks the pull request title, runs the unit
tests, builds and starts the image and lints and renders the chart. To run the
unit tests locally, with Python 3.9 (the version of the base image) and
[uv](https://docs.astral.sh/uv/) (pip 24.1 or newer rejects the metadata of a
dependency of FOCA 0.12):

```bash
uv venv --python 3.9
uv pip install -r requirements.txt --group dev
uv run --no-project pytest --cov
```

They need neither a cluster nor a Kubernetes configuration.

[pubgrade]: <https://github.com/elixir-cloud-aai/Pubgrade>