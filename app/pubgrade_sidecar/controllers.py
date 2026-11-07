"""Controllers of the API: read and update the image tags of Deployments."""

import base64
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
import hmac
import json
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
    Forbidden,
    ImageRepositoryMismatch,
    ImageUnavailable,
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
# The registry that serves Docker Hub's images, and the keys of its
# credentials in Docker's configuration files
DOCKER_HUB_REGISTRY = "registry-1.docker.io"
DOCKER_HUB_KEYS = (
    "https://index.docker.io/v1/",
    "index.docker.io",
    "docker.io",
    DOCKER_HUB_REGISTRY,
)
# Manifests and indexes of images, for multi-platform images too
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
# Largest response of a token service that is read, in bytes
MAX_TOKEN_RESPONSE = 256 * 1024
# Parameters of an authentication challenge (`WWW-Authenticate`)
CHALLENGE_PARAMETER = re.compile(r'(\w+)="([^"]*)"')
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
    """
    _check_token()
    image_repo, image_tag = _notification()
    deployment, _ = _read_deployment(deployment_name)
    plan = _plan_update(deployment, image_repo, image_tag)
    if plan is None:
        raise ImageRepositoryMismatch
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
        JSON response with the name and the new image of each deployment that
        rolls out, and status code 202; or, if all deployments that use the
        repository already use the image, with each of them and its image,
        and status code 200: then nothing is rolled out.

    Raises:
        Unauthorized: If the token is missing or wrong.
        BadRequest: If the tag is invalid.
        NotFound: If no container of these deployments uses the repository.
        Forbidden: If the sidecar has no right to read the deployments.
    """
    _check_token()
    image_repo, image_tag = _notification()
    plans = []
    for deployment in _managed_deployments():
        plan = _plan_update(deployment, image_repo, image_tag)
        if plan is not None:
            plans.append((deployment.metadata.name, *plan))
    if not plans:
        raise NotFound
    rolling_out = []
    for name, image, changes in plans:
        if _start_delivery(name, image, image_repo, image_tag, changes):
            rolling_out.append((name, image, changes))
    started = bool(rolling_out)
    message = (
        "Updating images in progress..."
        if started
        else "Deployments already use this image."
    )
    deployments = [
        {"name": name, "image": image}
        for name, image, _ in (rolling_out if started else plans)
    ]
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
    setting = os.getenv("DEPLOYMENT_LABEL", "").strip()
    if not setting:
        return deployments
    key, has_value, value = setting.partition("=")
    key, value = key.strip(), value.strip()
    if not key:
        # Matches none, rather than all deployments
        logger.error("DEPLOYMENT_LABEL has no label key; no deployment matches.")
        return []
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
    means `latest`, and one with a digest keeps it then) aren't changed; the
    others lose their digest, if any, as the new tag replaces it.

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

    The delivery plans its patch anew from the deployment as it is right
    before the patch (see `_deliver`); this plan is for the response.

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
    delivery = _Delivery(
        image=image,
        targets=[(index, new_image) for index, _, new_image in changes],
    )
    with _lock:
        # Last in the order of the records, as the newest notification
        _deliveries.pop(key, None)
        _deliveries[key] = delivery
    _executor.submit(_deliver, delivery, image_repo, image_tag, deployment_name)
    return True


def _json_patch(changes: list[tuple[int, str, str]]) -> list[dict[str, str]]:
    """Build the JSON patch for the images of containers.

    Each replacement is preceded by a test of the current image, so that a
    deployment changed since it was read fails the patch instead of getting
    an image in another container.

    Args:
        changes: Containers to change, each with its index, its current and
            its new image.

    Returns:
        The JSON patch.
    """
    patch = []
    for index, old_image, new_image in changes:
        path = f"/spec/template/spec/containers/{index}/image"
        patch += [
            {"op": "test", "value": old_image, "path": path},
            {"op": "replace", "value": new_image, "path": path},
        ]
    return patch


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
) -> None:
    """Wait for an image, then patch the deployment, and record the outcome.

    A delivery that a newer notification has replaced stops, also if it is
    replaced just before its patch (checked again under the deployment's
    patch lock). Under that lock, the deployment is read again and the patch
    planned anew, so that containers added, removed or reordered, or patched
    by an older delivery, during the wait count; a deployment that no longer
    uses the repository fails the delivery. The Kubernetes API is called
    outside the lock of the records, so that status reports don't wait for
    it.

    Args:
        delivery: Record of this delivery.
        image_repo: Canonical repository of the image.
        image_tag: Tag of the image.
        deployment_name: Name of the deployment.
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
            deployment, _ = _read_deployment(deployment_name)
            plan = _plan_update(deployment, image_repo, image_tag)
            if plan is None:
                _fail(delivery, "The deployment no longer uses the image repository.")
                return
            image, changes = plan
            with _lock:
                delivery.image = image
                if changes:
                    delivery.targets = [(index, new) for index, _, new in changes]
            patched: Any = deployment
            if changes:
                patched = apiV1.patch_namespaced_deployment(
                    name=deployment_name,
                    namespace=namespace,
                    body=_json_patch(changes),
                    _request_timeout=REQUEST_TIMEOUT,
                )
            with _lock:
                delivery.state = "patched"
                delivery.generation = patched.metadata.generation
                delivery.uid = patched.metadata.uid
    except ImageUnavailable:
        logger.warning("Image of a notification isn't available.")
        _fail(delivery, "Image not available, or its registry not reachable.")
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
        ImageUnavailable: If the image is still missing after all tries.
    """
    for attempt in range(MAX_RETRY + 1):
        if not _is_current(delivery, deployment_name, image_repo):
            return False
        if check_image_availability(image_repo, image_tag):
            return _is_current(delivery, deployment_name, image_repo)
        if attempt < MAX_RETRY:
            time.sleep(RETRY_INTERVAL)
    raise ImageUnavailable


def _registry(image_repo: str) -> tuple[str, str]:
    """Split a canonical repository into its registry and its name there.

    Args:
        image_repo: Canonical repository, see `_parse_image`.

    Returns:
        Host (with port, if any) of the registry and the repository's name in
        it; Docker Hub for repositories without a registry host.
    """
    if _is_on_docker_hub(image_repo):
        return DOCKER_HUB_REGISTRY, image_repo
    host, _, name = image_repo.partition("/")
    return host, name


def _credentials(host: str) -> tuple[str, str] | None:
    """Read the credentials for a registry.

    They come from a Docker configuration file (`auths`, with `auth` or
    `username` and `password` per registry) at the path in
    `REGISTRY_AUTH_FILE`, e.g., a mounted Secret of the type
    `kubernetes.io/dockerconfigjson`. Credential helpers and identity tokens
    aren't supported. For Docker Hub, the first usable of its keys counts.

    Args:
        host: Host of the registry.

    Returns:
        User name and password, or None if there are none for the registry.
    """
    path = os.getenv("REGISTRY_AUTH_FILE")
    if not path:
        return None
    keys = DOCKER_HUB_KEYS if host == DOCKER_HUB_REGISTRY else (host,)
    try:
        with open(path, encoding="utf-8") as stream:
            auths = json.load(stream).get("auths", {})
        entries = [(key, auths.get(key) or auths.get(f"https://{key}")) for key in keys]
    except (OSError, ValueError, AttributeError):
        # Without details, which could quote the file
        logger.warning("Registry credentials could not be read.")
        return None
    for key, entry in entries:
        if not entry:
            continue
        try:
            if entry.get("auth"):
                user, _, password = (
                    base64.b64decode(entry["auth"], validate=True)
                    .decode("utf-8")
                    .partition(":")
                )
                return user, password
            return entry["username"], entry["password"]
        except (ValueError, KeyError, AttributeError, TypeError):
            # The key is a registry host; the entry's content isn't logged
            logger.warning(
                "Registry credentials for %s could not be read: they need "
                "`auth`, or `username` and `password`.",
                key,
            )
    return None


def _insecure_registries() -> list[str]:
    """List the registries that are reached over HTTP.

    Returns:
        Hosts (with port, if any) in `INSECURE_REGISTRIES` (comma-separated).
    """
    return [
        entry.strip()
        for entry in os.getenv("INSECURE_REGISTRIES", "").split(",")
        if entry.strip()
    ]


def _bearer_token(fields: dict[str, str], host: str, scheme: str) -> str | None:
    """Request a token from the token service that a registry names.

    Credentials (see `_credentials`) are sent only to a token service over
    HTTPS, as with Docker's own client, or to an insecure registry itself.
    The response is read up to `MAX_TOKEN_RESPONSE` bytes.

    Args:
        fields: Parameters of the registry's challenge, with `realm`, the
            URL of the token service.
        host: Host of the registry.
        scheme: Scheme of the registry's URL.

    Returns:
        The token, or None if there is none.

    Raises:
        requests.RequestException: If the token service can't be reached.
        ValueError: If the response isn't JSON.
        AttributeError: If the response isn't a JSON object.
    """
    realm = fields["realm"]
    if not realm.startswith(("https://", "http://")):
        return None
    trusted = realm.startswith("https://") or (
        scheme == "http" and realm.startswith(f"http://{host}/")
    )
    token_response = requests.get(
        realm,
        params={key: fields[key] for key in ("service", "scope") if key in fields},
        auth=_credentials(host) if trusted else None,
        timeout=REQUEST_TIMEOUT,
        stream=True,
    )
    try:
        if token_response.status_code != 200:
            return None
        content = b""
        for chunk in token_response.iter_content(chunk_size=65536):
            content += chunk
            if len(content) > MAX_TOKEN_RESPONSE:
                return None
    finally:
        token_response.close()
    body = json.loads(content)
    token = body.get("token") or body.get("access_token")
    return token if isinstance(token, str) and token else None


def _head_manifest(url: str, host: str, scheme: str) -> requests.Response:
    """Ask a registry for a manifest, and authenticate if it asks for it.

    Redirects are followed; `requests` drops the authorization on redirects
    to other hosts.

    Args:
        url: URL of the manifest.
        host: Host of the registry.
        scheme: Scheme of the URL.

    Returns:
        The registry's last response.

    Raises:
        requests.RequestException: If the registry or its token service
            can't be reached.
        ValueError: If the token service's response isn't JSON.
        AttributeError: If the token service's response isn't a JSON object.
    """
    headers = {"Accept": MANIFEST_TYPES}
    response = requests.head(
        url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True
    )
    if response.status_code != 401:
        return response
    kind, _, parameters = response.headers.get("WWW-Authenticate", "").partition(" ")
    fields = dict(CHALLENGE_PARAMETER.findall(parameters))
    if kind.lower() == "bearer" and "realm" in fields:
        token = _bearer_token(fields, host, scheme)
        if token is None:
            return response
        return requests.head(
            url,
            headers={**headers, "Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
    credentials = _credentials(host) if kind.lower() == "basic" else None
    if credentials is None:
        return response
    return requests.head(
        url,
        headers=headers,
        auth=credentials,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )


def check_image_availability(image_repo: str, tag: str = "latest") -> bool:
    """Check whether the registry of an image has it.

    Asks the registry for the image's manifest (OCI distribution API), over
    HTTPS, or HTTP for the hosts in `INSECURE_REGISTRIES`, with a token or
    Basic authentication if the registry asks for it (see `_head_manifest`).

    Args:
        image_repo: Canonical repository of the image, see `_parse_image`.
        tag: Tag of the image.

    Returns:
        Whether the image is available. Network and authentication errors
        count as not available.
    """
    host, name = _registry(image_repo)
    scheme = "http" if host in _insecure_registries() else "https"
    url = f"{scheme}://{host}/v2/{name}/manifests/{tag}"
    try:
        response = _head_manifest(url, host, scheme)
    except (requests.RequestException, ValueError, AttributeError):
        return False
    return response.status_code == 200
