from kubernetes import client, config
from kubernetes.client.rest import ApiException
from dataclasses import dataclass
import hmac
import logging
import os
import re
import threading
from typing import Any, Dict, List, NoReturn, Optional, Tuple

from flask import request, jsonify
import requests
import time
from concurrent.futures import ThreadPoolExecutor
from pubgrade_sidecar.errors.exceptions import (
    BadRequest,
    DockerImageUnavailable,
    Forbidden,
    ImageRegistryUnsupported,
    ImageRepositoryMismatch,
    NotFound,
    Unauthorized,
)

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
REQUEST_TIMEOUT = 10
DOCKER_HUB_PREFIXES = ("docker.io/", "index.docker.io/")
# The format of Docker image tags
VALID_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


@dataclass
class _Delivery:
    """State of the delivery of a notification.

    Attributes:
        image: Image to be rolled out.
        state: `waiting_for_image`, `patched` or `failed`.
        detail: Explanation of a failure.
        generation: Generation of the Deployment that the patch resulted in.
        uid: Identifier of the Deployment that was patched.
    """

    image: str
    state: str = "waiting_for_image"
    detail: Optional[str] = None
    generation: int = 0
    uid: Optional[str] = None


# The last delivery per deployment, in memory: the sidecar has one replica. A
# restart loses a delivery that still waits for its image; for a patched one,
# the Deployment itself still tells the state of the rollout
_deliveries: Dict[str, _Delivery] = {}
_lock = threading.Lock()
# A sidecar serves few deployments, each with rare notifications
_executor = ThreadPoolExecutor(max_workers=4)


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


def _parse_image(image: str) -> Tuple[str, Optional[str]]:
    """Split an image into its canonical repository and its tag.

    Args:
        image: Image, e.g., `docker.io/org/app:1.0`.

    Returns:
        Repository and tag, if any. For Docker Hub, the repository has no
        registry prefix, and its official images are under `library/`.
    """
    name = image.split("@")[0]
    repository, tag = name, None
    if ":" in name.rsplit("/", 1)[-1]:
        repository, tag = name.rsplit(":", 1)
    for prefix in DOCKER_HUB_PREFIXES:
        if repository.startswith(prefix):
            repository = repository[len(prefix):]
            break
    if "/" not in repository:
        repository = f"library/{repository}"
    return repository, tag


def _is_on_docker_hub(repository: str) -> bool:
    """Tell whether a canonical repository is on Docker Hub.

    Args:
        repository: Canonical repository, see `_parse_image`.

    Returns:
        Whether its first part, unlike a registry host, has neither a dot nor
        a port.
    """
    first = repository.split("/")[0]
    return not ("." in first or ":" in first or first == "localhost")


def _raise_api_error(exc: ApiException) -> NoReturn:
    """Raise what an error of the Kubernetes API means for the caller.

    Args:
        exc: Error of the Kubernetes API.

    Raises:
        NotFound: If the object doesn't exist.
        Forbidden: If the sidecar has no right to access it.
        ApiException: For other errors, which are no client errors.
    """
    if exc.status == 404:
        raise NotFound
    if exc.status == 403:
        raise Forbidden
    raise exc


def _read_deployment(deployment_name: str) -> Tuple[Any, str]:
    """Read a deployment.

    Args:
        deployment_name: Name of the deployment.

    Returns:
        The deployment and the image of its first container.

    Raises:
        NotFound: If there is no such deployment, or it has no container.
        Forbidden: If the sidecar has no right to read it.
        ApiException: For other errors of the Kubernetes API, which are no
            client errors.
    """
    try:
        deployment = apiV1.read_namespaced_deployment(
            deployment_name, namespace, _request_timeout=REQUEST_TIMEOUT
        )
    except ApiException as exc:
        _raise_api_error(exc)
    try:
        return deployment, deployment.spec.template.spec.containers[0].image
    except IndexError:
        raise NotFound


def _rollout_status(deployment: Any) -> str:
    """Derive the state of a rollout from a Deployment.

    Args:
        deployment: Deployment as returned by the Kubernetes client.

    Returns:
        `failed`, `rolling_out` or `complete`, by the same rules as
        `kubectl rollout status`.
    """
    state = deployment.status
    if (state.observed_generation or 0) < deployment.metadata.generation:
        return "rolling_out"
    for condition in state.conditions or []:
        if (
            condition.type == "Progressing"
            and condition.reason == "ProgressDeadlineExceeded"
        ):
            return "failed"
    updated = state.updated_replicas or 0
    if (
        updated < deployment.spec.replicas
        or (state.replicas or 0) > updated
        or (state.available_replicas or 0) < updated
    ):
        return "rolling_out"
    return "complete"


def getDeployment():
    _check_token()
    try:
        pod_list = apiV1.list_namespaced_deployment(
            namespace, _request_timeout=REQUEST_TIMEOUT
        )
    except ApiException as exc:
        _raise_api_error(exc)
    pods = []
    for pod in pod_list.items:
        pods.append(pod.metadata.name)
    return pods


def getImage(deployment_name: str):
    """Report the image and the rollout status of a deployment.

    The record of the delivery is read before the Deployment, so that a
    delivery that was just patched can't be combined with an older Deployment.

    Args:
        deployment_name: Name of the deployment.

    Returns:
        JSON response with the image and the status. The last notification,
        if any, is known until the sidecar restarts.

    Raises:
        Unauthorized: If the token is missing or wrong.
        NotFound: If the deployment doesn't exist.
        Forbidden: If the sidecar has no right to read the deployment.
    """
    _check_token()
    with _lock:
        delivery = _deliveries.get(deployment_name)
        state = delivery.state if delivery else None
        detail = delivery.detail if delivery else None
        generation = delivery.generation if delivery else 0
        uid = delivery.uid if delivery else None
    deployment, image = _read_deployment(deployment_name)
    result = {
        "image": image,
        "status": _rollout_status(deployment),
        "replicas": deployment.spec.replicas,
        "updated_replicas": deployment.status.updated_replicas or 0,
        "available_replicas": deployment.status.available_replicas or 0,
    }
    if delivery is not None:
        result["requested_image"] = delivery.image
        # What the Deployment says counts once it has the requested image, or
        # once it has moved past the patch, e.g., by a rollback, or was recreated
        if state == "patched":
            if (
                deployment.metadata.uid == uid
                and deployment.metadata.generation < generation
            ):
                result["status"] = "rolling_out"
        elif delivery.image != image:
            result["status"] = state
            if detail:
                result["detail"] = detail
    return jsonify(result)


def deleteDeployment(deployment_name: str):
    _check_token()
    try:
        v1.delete_namespaced_pod(
            deployment_name, namespace, _request_timeout=REQUEST_TIMEOUT
        )
    except ApiException as exc:
        _raise_api_error(exc)
    return "deleted: " + deployment_name


def updateDeployment(deployment_name: str):
    """Accept a notification of a new image and start the delivery.

    Only the tag of the image can change. The delivery runs in the background;
    `GET /deployment/{deployment-name}` reports its state.

    Args:
        deployment_name: Name of the deployment.

    Returns:
        JSON response with the image and status code 202, or 200 if the
        deployment already uses that image: then nothing is rolled out.

    Raises:
        Unauthorized: If the token is missing or wrong.
        NotFound: If the deployment doesn't exist.
        Forbidden: If the sidecar has no right to read the deployment.
        BadRequest: If the tag is invalid.
        ImageRepositoryMismatch: If the deployment uses another repository.
        ImageRegistryUnsupported: If the image isn't on Docker Hub.
    """
    _check_token()
    image_tag = request.json["tag"]
    if not VALID_TAG.fullmatch(image_tag):
        raise BadRequest
    _, old_image = _read_deployment(deployment_name)

    image_repo = _parse_image(request.json["image_name"])[0]
    old_repo, old_tag = _parse_image(old_image)
    if old_repo != image_repo:
        raise ImageRepositoryMismatch
    if not _is_on_docker_hub(image_repo):
        raise ImageRegistryUnsupported

    # An image without a tag means `latest`, and one with a digest stays as it is
    if (old_tag or "latest") == image_tag:
        with _lock:
            _deliveries.pop(deployment_name, None)
        return jsonify(
            {"message": "Deployment already uses this image.", "image": old_image}
        ), 200

    # Keeps the deployment's spelling of the repository, so only the tag changes
    image = old_image.split("@")[0]
    if old_tag is not None:
        image = image.rsplit(":", 1)[0]
    image = f"{image}:{image_tag}"
    patch = [
        {
            "op": "replace",
            "value": image,
            "path": "/spec/template/spec/containers/0/image",
        }
    ]

    delivery = _Delivery(image=image)
    with _lock:
        _deliveries[deployment_name] = delivery
    _executor.submit(
        _deliver, delivery, image_repo, image_tag, deployment_name, patch
    )
    return jsonify(
        {"message": "Updating image in progress...", "image": image}
    ), 202


def _is_current(delivery: _Delivery, deployment_name: str) -> bool:
    """Tell whether no newer notification has replaced a delivery.

    Args:
        delivery: Record of the delivery.
        deployment_name: Name of the deployment.

    Returns:
        Whether the delivery is the last one of the deployment.
    """
    with _lock:
        return _deliveries.get(deployment_name) is delivery


def _deliver(
    delivery: _Delivery,
    image_repo: str,
    image_tag: str,
    deployment_name: str,
    patch: List[Dict[str, str]],
) -> None:
    """Wait for an image, then patch the deployment, and record the outcome.

    A delivery that a newer notification has replaced stops. The Kubernetes API
    is called outside the lock, so that status reports don't wait for it.

    Args:
        delivery: Record of this delivery.
        image_repo: Canonical repository of the image.
        image_tag: Tag of the image.
        deployment_name: Name of the deployment.
        patch: JSON patch for the deployment.
    """
    try:
        if not _wait_for_image(image_repo, image_tag, delivery, deployment_name):
            logger.info("Skipping a superseded delivery.")
            return
        patched = apiV1.patch_namespaced_deployment(
            name=deployment_name,
            namespace=namespace,
            body=patch,
            _request_timeout=REQUEST_TIMEOUT,
        )
        with _lock:
            delivery.state = "patched"
            delivery.generation = patched.metadata.generation
            delivery.uid = patched.metadata.uid
    except DockerImageUnavailable:
        logger.warning("Image of a notification isn't available.")
        _fail(delivery, "Image not available, or Docker Hub not reachable.")
    except Exception:
        logger.exception("Deployment could not be updated.")
        _fail(delivery, "Deployment could not be updated.")


def _fail(delivery: _Delivery, detail: str) -> None:
    """Record a failed delivery.

    Args:
        delivery: Record of the delivery.
        detail: Explanation for the status report.
    """
    with _lock:
        delivery.state = "failed"
        delivery.detail = detail


def _wait_for_image(
    image_repo: str,
    image_tag: str,
    delivery: _Delivery,
    deployment_name: str,
) -> bool:
    """Wait until an image is available, while a delivery is current.

    Args:
        image_repo: Canonical repository of the image.
        image_tag: Tag of the image.
        delivery: Record of the delivery.
        deployment_name: Name of the deployment.

    Returns:
        True once the image is available, False if the delivery was replaced.

    Raises:
        DockerImageUnavailable: If the image is still missing after all tries.
    """
    for attempt in range(MAX_RETRY + 1):
        if not _is_current(delivery, deployment_name):
            return False
        if check_docker_image_availability(image_repo, image_tag):
            return _is_current(delivery, deployment_name)
        if attempt < MAX_RETRY:
            time.sleep(RETRY_INTERVAL)
    raise DockerImageUnavailable


def check_docker_image_availability(image_name, tag='latest'):
    """Check whether Docker Hub has an image.

    Args:
        image_name: Repository of the image.
        tag: Tag of the image.

    Returns:
        Whether the image is available. Network errors count as not available.
    """
    base_url = 'https://hub.docker.com'
    url = f'{base_url}/v2/repositories/{image_name}/tags/{tag}/'
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return False
    return response.status_code == 200
