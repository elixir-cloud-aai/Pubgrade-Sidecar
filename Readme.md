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


[pubgrade]: <https://github.com/elixir-cloud-aai/Pubgrade>