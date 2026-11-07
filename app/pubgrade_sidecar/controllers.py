"""Controllers of the API: read and update the image tags of Deployments."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
import hmac
import logging
import os
import re
import threading
import time
from typing import Any, NoReturn, cast

from flask import Response, jsonify, request
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import requests

from pubgrade_sidecar.errors.exceptions import (
    BadRequest,
    DockerImageUnavailable,
    Forbidden,
    ImageRegistryUnsupported,
    ImageRepositoryMismatch,
    InternalServerError,
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
        targets: Containers that get the image, by index, with their new
            images; without them, the first container gets `image`.
    """

    image: str
    state: str = "waiting_for_image"
    detail: str | None = None
    generation: int = 0
    uid: str | None = None
    targets: list[tuple[int, str]] = field(default_factory=list)


# The last delivery per deployment and image repository, in memory: the
# sidecar has one replica. A restart loses a delivery that still waits for its
# image; for a patched one, the Deployment itself still tells the state of the
# rollout. A deployment with containers of several repositories has one
# delivery per repository, so that they don't replace each other
_deliveries: dict[tuple[str, str], _Delivery] = {}
_lock = threading.Lock()
# One lock per deployment, held from the last check that a delivery is current
# to the end of its patch, so that a delivery that a newer one replaced can't
# overwrite the newer one's image; status reports don't wait for it
_patch_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
# Each delivery holds a worker while it waits for its image (about two
# minutes at most); a notification for all deployments starts one delivery
# per deployment, and further ones wait for a free worker
_executor = ThreadPoolExecutor(max_workers=16)


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


def _parse_image(image: str) -> tuple[str, str | None]:
    """Split an image into its canonical repository and its tag.

    Args:
        image: Image, e.g., `docker.io/org/app:1.0`.

    Returns:
        Repository and tag, if any. For Docker Hub, the repository has no
        registry prefix, and its official images are under `library/`.
    """
    name = image.split("@", maxsplit=1)[0]
    repository, tag = name, None
    if ":" in name.rsplit("/", 1)[-1]:
        repository, tag = name.rsplit(":", 1)
    for prefix in DOCKER_HUB_PREFIXES:
        if repository.startswith(prefix):
            repository = repository[len(prefix) :]
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
    first = repository.split("/", maxsplit=1)[0]
    return not ("." in first or ":" in first or first == "localhost")


def _raise_api_error(exc: ApiException) -> NoReturn:
    """Raise what an error of the Kubernetes API means for the caller.

    Args:
        exc: Error of the Kubernetes API.

    Raises:
        NotFound: If the object doesn't exist.
        Forbidden: If the sidecar has no right to access it.
        InternalServerError: For other errors, which are no client errors
            (not with the status of the Kubernetes API, which FOCA would
            otherwise return, e.g., `503`).
    """
    if exc.status == 404:
        raise NotFound
    if exc.status == 403:
        raise Forbidden
    raise InternalServerError from exc


def _read_deployment(deployment_name: str) -> tuple[Any, str]:
    """Read a deployment.

    Args:
        deployment_name: Name of the deployment.

    Returns:
        The deployment and the image of its first container.

    Raises:
        NotFound: If there is no such deployment, or it has no container.
        Forbidden: If the sidecar has no right to read it.
        InternalServerError: For other errors of the Kubernetes API, which are
            no client errors.
    """
    # The client's models type all fields as optional; the API server sets
    # those that are read here
    deployment: Any
    try:
        deployment = apiV1.read_namespaced_deployment(
            deployment_name, namespace, _request_timeout=REQUEST_TIMEOUT
        )
    except ApiException as exc:
        _raise_api_error(exc)
    try:
        return deployment, deployment.spec.template.spec.containers[0].image
    except IndexError:
        raise NotFound from None


def _has_images(deployment: Any, delivery: _Delivery) -> bool:
    """Tell whether a deployment has the images that a delivery requested.

    Args:
        deployment: Deployment as returned by the Kubernetes client.
        delivery: Record of the delivery.

    Returns:
        Whether each target container of the delivery has its new image.
    """
    containers = deployment.spec.template.spec.containers
    targets = delivery.targets or [(0, delivery.image)]
    return all(
        index < len(containers) and containers[index].image == image
        for index, image in targets
    )


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


def getDeployment() -> list[str]:
    """List the deployments in the namespace.

    Returns:
        Names of the deployments.

    Raises:
        Unauthorized: If the token is missing or wrong.
        Forbidden: If the sidecar has no right to list the deployments.
    """
    _check_token()
    pod_list: Any
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


def getImage(deployment_name: str) -> Response:
    """Report the image and the rollout status of a deployment.

    The record of the delivery is read before the Deployment, so that a
    delivery that was just patched can't be combined with an older Deployment.

    Args:
        deployment_name: Name of the deployment.

    Returns:
        JSON response with the image and the status. The last notification
        per image repository, if any, is known until the sidecar restarts: a
        failed or waiting delivery of any of them counts over the
        Deployment's own status, a failed one first.

    Raises:
        Unauthorized: If the token is missing or wrong.
        NotFound: If the deployment doesn't exist.
        Forbidden: If the sidecar has no right to read the deployment.
    """
    _check_token()
    with _lock:
        # Copies, as deliveries change in the background
        deliveries = [
            replace(delivery)
            for (name, _), delivery in _deliveries.items()
            if name == deployment_name
        ]
    deployment, image = _read_deployment(deployment_name)
    result = {
        "image": image,
        "status": _rollout_status(deployment),
        "replicas": deployment.spec.replicas,
        "updated_replicas": deployment.status.updated_replicas or 0,
        "available_replicas": deployment.status.available_replicas or 0,
    }
    if deliveries:
        # The last notification, unless an earlier one (of another image
        # repository) explains the status
        reported = deliveries[-1]
        rank = 0
        for delivery in deliveries:
            # What the Deployment says counts once it has the requested images,
            # or once it has moved past the patch, e.g., by a rollback, or was
            # recreated
            if delivery.state == "patched":
                if (
                    deployment.metadata.uid == delivery.uid
                    and deployment.metadata.generation < delivery.generation
                    and rank < 1
                ):
                    reported, rank = delivery, 1
                    result["status"] = "rolling_out"
            elif not _has_images(deployment, delivery):
                delivery_rank = 3 if delivery.state == "failed" else 2
                if delivery_rank > rank:
                    reported, rank = delivery, delivery_rank
                    result["status"] = delivery.state
        result["requested_image"] = reported.image
        if rank > 1 and reported.detail:
            result["detail"] = reported.detail
    return jsonify(result)


def deleteDeployment(deployment_name: str) -> str:
    """Delete the pod of the given name.

    Args:
        deployment_name: Name of the pod.

    Returns:
        Confirmation of the deletion.

    Raises:
        Unauthorized: If the token is missing or wrong.
        NotFound: If the pod doesn't exist.
        Forbidden: If the sidecar has no right to delete the pod.
    """
    _check_token()
    try:
        v1.delete_namespaced_pod(
            deployment_name, namespace, _request_timeout=REQUEST_TIMEOUT
        )
    except ApiException as exc:
        _raise_api_error(exc)
    return "deleted: " + deployment_name


def updateDeployment(deployment_name: str) -> tuple[Response, int]:
    """Accept a notification of a new image and start the delivery.

    Every container of the deployment whose image repository equals the
    notification's changes, and only its tag. The delivery runs in the
    background; `GET /deployment/{deployment-name}` reports its state.

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
        ImageRepositoryMismatch: If no container of the deployment uses the
            repository.
        ImageRegistryUnsupported: If the image isn't on Docker Hub.
    """
    _check_token()
    image_repo, image_tag = _notification()
    deployment, _ = _read_deployment(deployment_name)
    plan = _plan_update(deployment, image_repo, image_tag)
    if plan is None:
        raise ImageRepositoryMismatch
    if not _is_on_docker_hub(image_repo):
        raise ImageRegistryUnsupported
    image, changes = plan
    if not _start_delivery(deployment_name, image, image_repo, image_tag, changes):
        return jsonify(
            {"message": "Deployment already uses this image.", "image": image}
        ), 200
    return jsonify({"message": "Updating image in progress...", "image": image}), 202


def updateDeployments() -> tuple[Response, int]:
    """Accept a notification of a new image for all deployments that use it.

    In every deployment that the sidecar manages, every container whose image
    repository equals the notification's changes, and only its tag. The
    sidecar manages the deployments named in the environment variable
    `TARGET_DEPLOYMENTS` (comma-separated), or all in its namespace without
    it; with `DEPLOYMENT_LABEL` (`key=value`, or `key` for any value), only
    those with this label. Listed deployments that don't exist are skipped.
    Each deployment gets its own delivery, whose state
    `GET /deployment/{deployment-name}` reports.

    Returns:
        JSON response with the name and the (new) image of each deployment
        that uses the repository, and status code 202, or 200 if all of them
        already use the image: then nothing is rolled out.

    Raises:
        Unauthorized: If the token is missing or wrong.
        BadRequest: If the tag is invalid.
        ImageRegistryUnsupported: If the image isn't on Docker Hub.
        NotFound: If no container of these deployments uses the repository.
        Forbidden: If the sidecar has no right to read the deployments.
    """
    _check_token()
    image_repo, image_tag = _notification()
    if not _is_on_docker_hub(image_repo):
        raise ImageRegistryUnsupported
    plans = []
    for deployment in _managed_deployments():
        plan = _plan_update(deployment, image_repo, image_tag)
        if plan is not None:
            plans.append((deployment.metadata.name, *plan))
    if not plans:
        raise NotFound
    started = False
    for name, image, changes in plans:
        started |= _start_delivery(name, image, image_repo, image_tag, changes)
    message = (
        "Updating images in progress..."
        if started
        else "Deployments already use this image."
    )
    deployments = [{"name": name, "image": image} for name, image, _ in plans]
    return jsonify({"message": message, "deployments": deployments}), (
        202 if started else 200
    )


def _notification() -> tuple[str, str]:
    """Read the notification of the current request.

    Returns:
        Canonical repository and tag of the image.

    Raises:
        BadRequest: If the tag is invalid.
    """
    # The API's validation ensures a JSON object with both fields
    body = cast(dict[str, str], request.json)
    image_tag = body["tag"]
    if not VALID_TAG.fullmatch(image_tag):
        raise BadRequest
    return _parse_image(body["image_name"])[0], image_tag


def _managed_deployments() -> list[Any]:
    """Read the deployments that the sidecar manages.

    Returns:
        The deployments named in `TARGET_DEPLOYMENTS`, or all in the
        namespace without it, that carry the label in `DEPLOYMENT_LABEL`, if
        set. Named deployments that don't exist are left out.

    Raises:
        Forbidden: If the sidecar has no right to read the deployments.
        InternalServerError: For other errors of the Kubernetes API.
    """
    names = [
        name.strip()
        for name in os.getenv("TARGET_DEPLOYMENTS", "").split(",")
        if name.strip()
    ]
    deployments: list[Any] = []
    try:
        if not names:
            listed: Any = apiV1.list_namespaced_deployment(
                namespace, _request_timeout=REQUEST_TIMEOUT
            )
            deployments = list(listed.items)
        for name in names:
            try:
                deployments.append(
                    apiV1.read_namespaced_deployment(
                        name, namespace, _request_timeout=REQUEST_TIMEOUT
                    )
                )
            except ApiException as exc:
                if exc.status != 404:
                    raise
                logger.warning("Deployment %s not found; skipped.", name)
    except ApiException as exc:
        _raise_api_error(exc)
    key, has_value, value = os.getenv("DEPLOYMENT_LABEL", "").partition("=")
    key, value = key.strip(), value.strip()
    if not key:
        return deployments
    return [
        deployment
        for deployment in deployments
        if key in (deployment.metadata.labels or {})
        and (not has_value or deployment.metadata.labels[key] == value)
    ]


def _plan_update(
    deployment: Any, image_repo: str, image_tag: str
) -> tuple[str, list[tuple[int, str, str]]] | None:
    """Plan the update of the containers that use an image repository.

    Each container keeps its spelling of the repository, so only the tag
    changes. Containers that use the tag already (an image without a tag
    means `latest`, and one with a digest stays as it is) aren't changed.

    Args:
        deployment: Deployment as returned by the Kubernetes client.
        image_repo: Canonical repository of the image.
        image_tag: Tag of the image.

    Returns:
        The image of the first container of the repository after the update,
        and the containers to change (empty if none needs to), each with its
        index, its current and its new image; None if no container uses the
        repository.
    """
    image = None
    changes = []
    for index, container in enumerate(deployment.spec.template.spec.containers):
        old_repo, old_tag = _parse_image(container.image)
        if old_repo != image_repo:
            continue
        new_image = container.image
        if (old_tag or "latest") != image_tag:
            new_image = container.image.split("@")[0]
            if old_tag is not None:
                new_image = new_image.rsplit(":", 1)[0]
            new_image = f"{new_image}:{image_tag}"
            changes.append((index, container.image, new_image))
        if image is None:
            image = new_image
    if image is None:
        return None
    return image, changes


def _start_delivery(
    deployment_name: str,
    image: str,
    image_repo: str,
    image_tag: str,
    changes: list[tuple[int, str, str]],
) -> bool:
    """Start the delivery of an image to a deployment, if it needs one.

    The JSON patch first tests that each container still has the image it was
    planned for, so that a deployment changed during the wait for the image
    fails the delivery instead of getting an image in another container.

    Args:
        deployment_name: Name of the deployment.
        image: Image of the first container of the repository after the update.
        image_repo: Canonical repository of the image.
        image_tag: Tag of the image.
        changes: Containers to change, each with its index, its current and
            its new image; none if the deployment uses the image already,
            which ends an earlier delivery that still waits.

    Returns:
        Whether a delivery started.
    """
    key = (deployment_name, image_repo)
    if not changes:
        with _lock:
            _deliveries.pop(key, None)
        return False
    patch = []
    for index, old_image, new_image in changes:
        path = f"/spec/template/spec/containers/{index}/image"
        patch += [
            {"op": "test", "value": old_image, "path": path},
            {"op": "replace", "value": new_image, "path": path},
        ]
    delivery = _Delivery(
        image=image,
        targets=[(index, new_image) for index, _, new_image in changes],
    )
    with _lock:
        # Last in the order of the records, as the newest notification
        _deliveries.pop(key, None)
        _deliveries[key] = delivery
    _executor.submit(_deliver, delivery, image_repo, image_tag, deployment_name, patch)
    return True


def _is_current(delivery: _Delivery, deployment_name: str, image_repo: str) -> bool:
    """Tell whether no newer notification has replaced a delivery.

    Args:
        delivery: Record of the delivery.
        deployment_name: Name of the deployment.
        image_repo: Canonical repository of the image.

    Returns:
        Whether the delivery is the last one of the deployment for the image
        repository.
    """
    with _lock:
        return _deliveries.get((deployment_name, image_repo)) is delivery


def _deliver(
    delivery: _Delivery,
    image_repo: str,
    image_tag: str,
    deployment_name: str,
    patch: list[dict[str, str]],
) -> None:
    """Wait for an image, then patch the deployment, and record the outcome.

    A delivery that a newer notification has replaced stops, also if it is
    replaced just before its patch (checked again under the deployment's
    patch lock). The Kubernetes API is called outside the lock of the
    records, so that status reports don't wait for it.

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
        with _lock:
            patch_lock = _patch_locks[deployment_name]
        with patch_lock:
            if not _is_current(delivery, deployment_name, image_repo):
                logger.info("Skipping a superseded delivery.")
                return
            patched: Any = apiV1.patch_namespaced_deployment(
                name=deployment_name,
                namespace=namespace,
                body=_current_tests(deployment_name, patch),
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


def _current_tests(
    deployment_name: str, patch: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Update the tests of a JSON patch to the deployment as it is now.

    A delivery that patched the deployment since the notification was planned
    (e.g., an older one of the same image repository) changed the tag of the
    containers to update: their tests then expect the current image, as long
    as it is of the same repository. A container that now has another
    repository (e.g., after the containers were reordered) keeps the planned
    test, which fails the patch.

    Args:
        deployment_name: Name of the deployment.
        patch: JSON patch for the deployment.

    Returns:
        The patch, with the tests updated.
    """
    deployment, _ = _read_deployment(deployment_name)
    containers = deployment.spec.template.spec.containers
    updated = []
    for operation in patch:
        current = operation
        if operation["op"] == "test":
            index = int(operation["path"].split("/")[-2])
            if index < len(containers):
                image = containers[index].image
                if _parse_image(image)[0] == _parse_image(operation["value"])[0]:
                    current = {**operation, "value": image}
        updated.append(current)
    return updated


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
        if not _is_current(delivery, deployment_name, image_repo):
            return False
        if check_docker_image_availability(image_repo, image_tag):
            return _is_current(delivery, deployment_name, image_repo)
        if attempt < MAX_RETRY:
            time.sleep(RETRY_INTERVAL)
    raise DockerImageUnavailable


def check_docker_image_availability(image_name: str, tag: str = "latest") -> bool:
    """Check whether Docker Hub has an image.

    Args:
        image_name: Repository of the image.
        tag: Tag of the image.

    Returns:
        Whether the image is available. Network errors count as not available.
    """
    base_url = "https://hub.docker.com"
    url = f"{base_url}/v2/repositories/{image_name}/tags/{tag}/"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return False
    return response.status_code == 200
