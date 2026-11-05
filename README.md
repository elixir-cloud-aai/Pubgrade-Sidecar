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

- Get available deployments in the namespace
 ```bash
 curl --location --request GET 'http://{pubgrade_sidecar-url}/deployment'
 ```

- Get current image of the deployment.
```bash
curl --location --request GET 'http://{pubgrade_sidecar-url}/deployment/
{deployment-name}' \
--header 'X-Access-Token: {access-token (specified at config.yaml)}'
```

- Modify image tag of the deployment.
```bash
curl --location --request PUT 'http://{pubgrade_sidecar-url}/deployment/
{deployment-name}' \
--header 'X-Access-Token: {access-token (specified at config.yaml)}' \
--header 'Content-Type: application/json' \
--data-raw '{
    "image_name": "akash7778/pubgrade",
    "tag": "latest"
}'
```

## Development

CI (`.github/workflows/ci.yml`) checks the pull request title, runs the unit
tests, builds the image and lints and renders the chart. To run the unit tests
locally, with Python 3.9 (the version of the base image) and
[uv](https://docs.astral.sh/uv/) (pip 24.1 or newer rejects the metadata of a
dependency of FOCA 0.12):

```bash
uv venv --python 3.9
uv pip install -r requirements.txt --group dev
uv run --no-project pytest --cov
```

They need neither a cluster nor a Kubernetes configuration.

[pubgrade]: <https://github.com/elixir-cloud-aai/Pubgrade>