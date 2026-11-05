from kubernetes import client, config
import hmac
import logging
import os
import re

from flask import request, jsonify
import requests
import time
from concurrent.futures import ThreadPoolExecutor
from pubgrade_sidecar.errors.exceptions import Unauthorized, NotFound, DockerImageUnavailable

namespace = os.getenv("NAMESPACE", "pubgrade_sidecar")

logger = logging.getLogger(__name__)


def _load_kube_config() -> None:
    """Load the configuration of the Kubernetes client.

    In a cluster, the configuration of the pod is required, so that a sidecar
    that can't work fails at startup. Outside a cluster, e.g., for the unit
    tests, a missing configuration is only a warning.

    Raises:
        kubernetes.config.ConfigException: In a cluster, if the configuration
            can't be loaded.
    """
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()
        return
    try:
        config.load_kube_config()
    except config.ConfigException as exc:
        logger.warning(
            "Kubernetes configuration not loaded, API calls will fail: %s", exc
        )


_load_kube_config()

v1 = client.CoreV1Api()
apiV1 = client.AppsV1Api()
MAX_RETRY = 3
RETRY_INTERVAL = 30




def _check_token() -> None:
    """Check the access token of the current request.

    The token comes from the environment variable `ACCESS_TOKEN`. Without it,
    all requests are rejected.

    Raises:
        Unauthorized: If the token is missing or wrong, or none is configured.
    """
    expected = os.getenv("ACCESS_TOKEN", "")
    token = request.headers.get("X-Access-Token", "")
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise Unauthorized


def getDeployment():
    _check_token()
    pod_list = apiV1.list_namespaced_deployment(namespace)
    pods = []
    for pod in pod_list.items:
        pods.append(pod.metadata.name)
    return pods


def getImage(deployment_name: str):
    _check_token()

    try:
        deployment = apiV1.read_namespaced_deployment(
            deployment_name, namespace
        )
        return deployment.spec.template.spec.containers[0].image
    except Exception:
        raise NotFound


def deleteDeployment(deployment_name: str):
    _check_token()

    try:
        v1.delete_namespaced_pod(
            deployment_name, namespace
        )
    except Exception:
        raise NotFound
    return "deleted: " + deployment_name


def updateDeployment(deployment_name: str):
    _check_token()

    image_repo = request.json["image_name"]
    image_tag = request.json["tag"]
    old_image = getImage(deployment_name)
    image = image_repo + ":" + image_tag
    if re.split(":", old_image)[0] != re.split(":", image)[0]:
        return "Cannot change image, only tag"

    patch = [
        {
            "op": "replace",
            "value": image,
            "path": "/spec/template/spec/containers/0/image",
        }
    ]

    # TODO: Re write retry logic with kafka pub-sub queue, separating retry from server process into a different pod
    with ThreadPoolExecutor() as executor:
        executor.submit(
            update_image, image_repo, image_tag, deployment_name, patch, 0
        )
        return jsonify({"message": "Updating image in progress..."})


def update_image(image_repo, image_tag, deployment_name, patch, retry):
    if retry > MAX_RETRY:
        raise DockerImageUnavailable
    if check_docker_image_availability(image_repo, image_tag):
        return apiV1.patch_namespaced_deployment(
            name=deployment_name, namespace=namespace, body=patch
        )
    else:
        time.sleep(RETRY_INTERVAL)
        update_image(image_repo, image_tag, deployment_name, patch, retry + 1)


def check_docker_image_availability(image_name, tag='latest'):
    base_url = 'https://hub.docker.com'
    url = f'{base_url}/v2/repositories/{image_name}/tags/{tag}/'
    response = requests.get(url)
    return response.status_code == 200
