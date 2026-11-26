"""Controllers of the API: read and update the image tags of Deployments."""

import base64
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC
from email.utils import parsedate_to_datetime
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from typing import Any, NoReturn, cast
from urllib.parse import urlsplit

from flask import Response, jsonify, request
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from packaging.version import InvalidVersion, Version
import requests

from pubgrade_sidecar.errors.exceptions import (
    DOWNGRADE_DETAIL,
    BadRequest,
    DowngradeRefused,
    Forbidden,
    ImageRepositoryMismatch,
    ImageUnavailable,
    InternalServerError,
    NotFound,
    TagNotAllowed,
    Unauthorized,
)

namespace = os.getenv("NAMESPACE", "pubgrade_sidecar")

logger = logging.getLogger(__name__)


def _load_kube_config() -> None:
    """Load the configuration of the Kubernetes client.

    In a cluster, the configuration of the pod is required, so that a sidecar
    that can't work fails at startup. Outside a cluster, the kubeconfig must be
    given explicitly in `KUBECONFIG`: the default one (`~/.kube/config`) and
    its active context are refused, so that the sidecar never acts on whatever
    cluster happens to be active. A file that is missing, e.g., a mistyped
    path, is refused too; one that exists but can't be loaded, e.g., an empty
    file for running without a cluster, is only a warning.

    Raises:
        kubernetes.config.ConfigException: In a cluster, if the configuration
            can't be loaded; outside a cluster, if `KUBECONFIG` is unset or
            empty, or names a file that doesn't exist.
    """
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()
        return
    # Passed on explicitly: the client reads `KUBECONFIG` only once, on import
    kubeconfig = os.getenv("KUBECONFIG", "").strip()
    if not kubeconfig:
        raise config.ConfigException(
            "Not running in a cluster and no kubeconfig given: refusing to use "
            "the default kubeconfig (~/.kube/config) and its active context. "
            "Set KUBECONFIG to the kubeconfig of the cluster to manage, or to "
            "an empty file to run without a cluster."
        )
    # Separated by `:`, as the client splits it
    missing = [
        path
        for path in kubeconfig.split(":")
        if path and not os.path.exists(os.path.expanduser(path))
    ]
    if missing:
        raise config.ConfigException(
            f"KUBECONFIG names files that don't exist: {', '.join(missing)}"
        )
    try:
        config.load_kube_config(config_file=kubeconfig)
    except config.ConfigException as exc:
        logger.warning(
            "Kubernetes configuration not loaded, API calls will fail: %s", exc
        )


_load_kube_config()

apiV1 = client.AppsV1Api()
# Defaults of the wait for a notified image: checks after the first, and
# seconds between them (`IMAGE_WAIT_RETRIES`, `IMAGE_WAIT_INTERVAL`), and the
# largest values that are accepted, as in the chart
IMAGE_WAIT_RETRIES = 3
IMAGE_WAIT_INTERVAL = 30.0
MAX_IMAGE_WAIT_RETRIES = 100
MAX_IMAGE_WAIT_INTERVAL = 3600.0
# Longest single sleep of a pause between checks: after each, a delivery that
# a newer one replaced stops, so that it frees its worker
PAUSE_STEP = 5.0
REQUEST_TIMEOUT = 10
# Hosts of Docker Hub in image names, as Pubgrade counts them
DOCKER_HUB_PREFIXES = ("docker.io/", "index.docker.io/", "registry-1.docker.io/")
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
TOKEN_CHUNK_SIZE = 4096
# Parameters of an authentication challenge (`WWW-Authenticate`)
CHALLENGE_PARAMETER = re.compile(r'(\w+)="([^"]*)"')
# The format of Docker image tags
VALID_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
# Headers that the reason of a failed check quotes, in this order, with the
# syntax that their values must have: others, e.g., with URLs, line breaks or
# longer ones, are left out, so that a registry can't write into the logs.
# `Retry-After` is seconds or an HTTP date; the rate-limit headers are
# numbers with an optional window (`100;w=3600`), as Docker Hub sends them
QUOTED_HEADERS = {
    "retry-after": re.compile(
        r"[0-9]{1,12}|[A-Z][a-z]{2}, [0-9]{2} [A-Z][a-z]{2} [0-9]{4} "
        r"[0-9]{2}:[0-9]{2}:[0-9]{2} (GMT|[+-][0-9]{4})"
    ),
    "ratelimit-limit": re.compile(r"[0-9]{1,12}(;w=[0-9]{1,12})?"),
    "ratelimit-remaining": re.compile(r"[0-9]{1,12}(;w=[0-9]{1,12})?"),
    "ratelimit-reset": re.compile(r"[0-9]{1,12}"),
}
# `Retry-After` in seconds (otherwise, it is an HTTP date)
DELAY_SECONDS = re.compile(r"[0-9]{1,12}")
# Answers of registries after which `Retry-After` counts
RETRY_STATUSES = frozenset({429, 503})
# Explanations of the answers of registries, for the reason of a failed check
STATUS_HINTS = {
    401: "authentication failed",
    403: "access denied",
    404: "image or tag not found",
    429: "rate limit reached",
    503: "service unavailable",
}
# Values of `REFUSE_DOWNGRADES`; any other value counts as on
SETTING_ON = frozenset({"1", "true", "yes", "on"})
SETTING_OFF = frozenset({"", "0", "false", "no", "off"})


@dataclass(frozen=True)
class ImageCheck:
    """Outcome of a check whether a registry has an image.

    Attributes:
        available: Whether the registry has the image.
        reason: Why it isn't available, e.g., "registry answered 429 (rate
            limit reached; ratelimit-remaining: 0)": the status and the
            registry's `Retry-After` and rate-limit headers, or the class of
            the network error. Never tokens, credentials, URLs or error
            messages, so that it can be logged and reported.
        retry_after: Seconds that the registry asks to wait before the next
            request (`Retry-After` of a 429 or 503), if it says so.
    """

    available: bool
    reason: str | None = None
    retry_after: float | None = None


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
# Each delivery holds a worker while it waits for its image (by default
# about 90 s, see `_image_wait`); a notification for all deployments starts
# one delivery per deployment, and further ones wait for a free worker
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


def _has_tag_or_digest(image: str) -> bool:
    """Tell whether an image names a tag or a digest, as `_parse_image` reads it.

    Args:
        image: Image, e.g., `registry:5000/org/app` (a port is no tag).

    Returns:
        Whether it has a digest (`@`), or a tag (`:` in its last part).
    """
    return "@" in image or _parse_image(image)[1] is not None


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
    """List the deployments that the sidecar manages.

    Returns:
        Names of the deployments, as `PUT /deployment` selects them (see
        `_managed_deployments`).

    Raises:
        Unauthorized: If the token is missing or wrong.
        Forbidden: If the sidecar has no right to read the deployments.
    """
    _check_token()
    return [deployment.metadata.name for deployment in _managed_deployments()]


def _target_names() -> list[str]:
    """Read the names of the deployments that the sidecar may use.

    Returns:
        The names in `TARGET_DEPLOYMENTS` (comma-separated), each once, in
        their order; empty if it isn't set, for all deployments in the
        namespace.
    """
    names = (name.strip() for name in os.getenv("TARGET_DEPLOYMENTS", "").split(","))
    return list(dict.fromkeys(name for name in names if name))


def _check_target(deployment_name: str) -> None:
    """Check that the sidecar may use a deployment.

    Args:
        deployment_name: Name of the deployment.

    Raises:
        Forbidden: If `TARGET_DEPLOYMENTS` is set and doesn't name it.
    """
    names = _target_names()
    if names and deployment_name not in names:
        raise Forbidden


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
        Forbidden: If `TARGET_DEPLOYMENTS` doesn't name the deployment, or the
            sidecar has no right to read it.
    """
    _check_token()
    _check_target(deployment_name)
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
        Forbidden: If `TARGET_DEPLOYMENTS` doesn't name the deployment, or the
            sidecar has no right to read it.
        BadRequest: If the tag is invalid, or the image name has a tag or a
            digest.
        ImageRepositoryMismatch: If no container of the deployment uses the
            repository.
        TagNotAllowed: If the tag doesn't match `ALLOWED_TAGS`.
        DowngradeRefused: With `REFUSE_DOWNGRADES`, if the tag isn't an
            upgrade of each container it would change (see `_check_upgrade`),
            or would undercut a waiting delivery (see `_undercuts_waiting`).
        InternalServerError: If `ALLOWED_TAGS` isn't a regular expression.
    """
    _check_token()
    _check_target(deployment_name)
    image_repo, image_tag = _notification()
    deployment, _ = _read_deployment(deployment_name)
    plan = _plan_update(deployment, image_repo, image_tag)
    if plan is None:
        raise ImageRepositoryMismatch
    image, changes = plan
    if not _start_deliveries(
        image_repo, image_tag, [(deployment_name, image, changes)]
    ):
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
        BadRequest: If the tag is invalid, or the image name has a tag or a
            digest.
        NotFound: If no container of these deployments uses the repository.
        Forbidden: If the sidecar has no right to read the deployments.
        TagNotAllowed: If the tag doesn't match `ALLOWED_TAGS`.
        DowngradeRefused: With `REFUSE_DOWNGRADES`, if the tag isn't an
            upgrade of each container it would change in any of the
            deployments, or would undercut a waiting delivery of any of them;
            then no deployment rolls out.
        InternalServerError: If `ALLOWED_TAGS` isn't a regular expression.
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
    rolling_out = _start_deliveries(image_repo, image_tag, plans)
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
        BadRequest: If the tag is invalid, or the image name has a tag or a
            digest (which would be dropped).
        TagNotAllowed: If the tag doesn't match `ALLOWED_TAGS`.
        InternalServerError: If `ALLOWED_TAGS` isn't a regular expression.
    """
    # The API's validation ensures a JSON object with both fields
    body = cast(dict[str, str], request.json)
    image_name, image_tag = body["image_name"], body["tag"]
    if not VALID_TAG.fullmatch(image_tag):
        raise BadRequest
    if _has_tag_or_digest(image_name):
        raise BadRequest
    _check_allowed_tag(image_tag)
    return _parse_image(image_name)[0], image_tag


def _check_allowed_tag(image_tag: str) -> None:
    """Check a tag against the pattern of allowed tags, if one is set.

    The pattern is the regular expression (Python syntax) in `ALLOWED_TAGS`;
    the whole tag must match it. Without it, or if it is empty, any tag is
    allowed.

    Args:
        image_tag: Tag of a notification.

    Raises:
        TagNotAllowed: If the tag doesn't match the pattern.
        InternalServerError: If the pattern isn't a regular expression, so
            that no tag is allowed until it is fixed.
    """
    pattern = os.getenv("ALLOWED_TAGS", "").strip()
    if not pattern:
        return
    try:
        allowed = re.compile(pattern)
    except re.error:
        logger.error("ALLOWED_TAGS is no regular expression; all tags refused.")
        raise InternalServerError from None
    if not allowed.fullmatch(image_tag):
        raise TagNotAllowed


def _refuse_downgrades() -> bool:
    """Tell whether notifications of lower versions are refused.

    Returns:
        Whether `REFUSE_DOWNGRADES` is on (`true`, `1`, `yes` or `on`, in any
        case). Values that are neither on nor off (`false`, `0`, `no`, `off`
        or empty) count as on, so that a typo doesn't allow downgrades.
    """
    value = os.getenv("REFUSE_DOWNGRADES", "").strip().lower()
    if value in SETTING_OFF:
        return False
    if value not in SETTING_ON:
        logger.error("REFUSE_DOWNGRADES has an unknown value; downgrades refused.")
    return True


def _version(tag: str | None) -> Version | None:
    """Read a tag as a version.

    Versions follow PEP 440, which also reads SemVer's pre-releases, e.g.,
    `1.2.0-rc.1` as `1.2.0rc1`, and allows a leading `v`. Post-releases count
    only as `.postN` (`1.2.0.post1`): others, e.g., `1.2.0-1`, `1.2.0-r1` or
    `1.2.0_r1`, are refused, as SemVer reads those with a hyphen as
    pre-releases, i.e., lower versions, and the others often name rebuilds or
    flavours of a release rather than later releases.

    Args:
        tag: Tag of an image, if any.

    Returns:
        The version, or None if the tag is missing, isn't a version, or is
        ambiguous.
    """
    if tag is None:
        return None
    try:
        version = Version(tag)
    except InvalidVersion:
        return None
    if version.post is not None and f".post{version.post}" not in tag.lower():
        return None
    return version


def _is_upgrade(old_tag: str | None, new_tag: str) -> bool:
    """Tell whether a new tag is a higher version than an old one.

    Both must be versions (see `_version`) with the same number of release
    segments, e.g., `1.2.0` and `1.3.0`, not `1.2` and `1.2.0` (a tag like
    `1.2` often moves along its patch releases, so `1.2.0` may be older) or
    `1.2.0` and `20230101` (a build number or a date). The new version must
    be higher; the same version spelled otherwise (`v1.2.0`) is no upgrade.

    Args:
        old_tag: Current tag, if any.
        new_tag: Tag of the notification.

    Returns:
        Whether the new tag is an upgrade.
    """
    old, new = _version(old_tag), _version(new_tag)
    return (
        old is not None
        and new is not None
        and len(old.release) == len(new.release)
        and new > old
    )


def _check_upgrade(changes: list[tuple[int, str, str]], image_tag: str) -> None:
    """Check that changes of containers are upgrades.

    Args:
        changes: Containers to change, each with its index, its current and
            its new image.
        image_tag: Tag of the notification.

    Raises:
        DowngradeRefused: If the tag isn't an upgrade (see `_is_upgrade`) of
            the current tag of each container to change. A digest next to a
            tag doesn't count.
    """
    for _, old_image, _ in changes:
        if not _is_upgrade(_parse_image(old_image)[1], image_tag):
            raise DowngradeRefused


def _undercuts_waiting(key: tuple[str, str], image_tag: str) -> bool:
    """Tell whether a notification would undercut a delivery that waits.

    A notification that would end or replace a delivery of the same
    deployment and repository that still waits for its image must name that
    delivery's tag or an upgrade of it (see `_is_upgrade`); otherwise, e.g., a
    notification of the current tag could cancel a waiting security release.
    The caller holds `_lock`, so that no delivery starts between this check
    and the replacement.

    Args:
        key: Name of the deployment and canonical repository of the image.
        image_tag: Tag of the notification.

    Returns:
        Whether a waiting delivery has another tag, of which this one is no
        upgrade.
    """
    delivery = _deliveries.get(key)
    if delivery is None or delivery.state != "waiting_for_image":
        return False
    waiting_tag = _parse_image(delivery.image)[1]
    return waiting_tag != image_tag and not _is_upgrade(waiting_tag, image_tag)


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
    names = _target_names()
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

    Raises:
        DowngradeRefused: With `REFUSE_DOWNGRADES`, if any change would be a
            downgrade (see `_check_upgrade`).
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
    if _refuse_downgrades():
        _check_upgrade(changes, image_tag)
    return image, changes


def _start_deliveries(
    image_repo: str,
    image_tag: str,
    plans: list[tuple[str, str, list[tuple[int, str, str]]]],
) -> list[tuple[str, str, list[tuple[int, str, str]]]]:
    """Start the deliveries of an image to deployments that need one.

    Each delivery plans its patch anew from the deployment as it is right
    before the patch (see `_deliver`); these plans are for the response. A
    deployment without changes ends an earlier delivery that still waits.
    With `REFUSE_DOWNGRADES`, no delivery starts or ends if the notification
    would undercut a waiting delivery of any of the deployments (see
    `_undercuts_waiting`), checked under the same hold of `_lock` as the
    replacements.

    Args:
        image_repo: Canonical repository of the image.
        image_tag: Tag of the image.
        plans: Per deployment, its name, the image of its first container of
            the repository after the update, and the containers to change,
            each with its index, its current and its new image.

    Returns:
        The plans of the deployments whose delivery started.

    Raises:
        DowngradeRefused: If the notification would undercut a waiting
            delivery.
    """
    refuse = _refuse_downgrades()
    started = []
    with _lock:
        if refuse and any(
            _undercuts_waiting((name, image_repo), image_tag) for name, _, _ in plans
        ):
            raise DowngradeRefused
        for name, image, changes in plans:
            key = (name, image_repo)
            # Last in the order of the records, as the newest notification
            _deliveries.pop(key, None)
            if changes:
                delivery = _Delivery(
                    image=image,
                    targets=[(index, new_image) for index, _, new_image in changes],
                )
                _deliveries[key] = delivery
                started.append((name, image, changes, delivery))
    for name, _, _, delivery in started:
        _executor.submit(_deliver, delivery, image_repo, image_tag, name)
    return [(name, image, changes) for name, image, changes, _ in started]


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
    uses the repository fails the delivery, and so does one that would be a
    downgrade by then, with `REFUSE_DOWNGRADES`. The Kubernetes API is called
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
    except ImageUnavailable as exc:
        suffix = f": {exc.reason}" if exc.reason else ""
        logger.warning("Image %s:%s isn't available%s.", image_repo, image_tag, suffix)
        _fail(delivery, f"Image not available, or its registry not reachable{suffix}.")
    except DowngradeRefused:
        # The deployment got a higher version during the wait
        logger.warning("Delivery refused, as it would be a downgrade.")
        _fail(delivery, DOWNGRADE_DETAIL)
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


def _image_wait() -> tuple[int, float]:
    """Read the settings of the wait for a notified image.

    `IMAGE_WAIT_RETRIES` is the number of checks after the first (a whole
    number, 0 or more), `IMAGE_WAIT_INTERVAL` the seconds between them (a
    number above 0). An invalid or empty setting keeps its default, with a
    warning for an invalid one.

    Returns:
        The number of checks after the first, and the seconds between them.
    """
    retries = IMAGE_WAIT_RETRIES
    value = os.getenv("IMAGE_WAIT_RETRIES", "").strip()
    if value:
        try:
            retries = int(value)
        except ValueError:
            retries = -1
        if not 0 <= retries <= MAX_IMAGE_WAIT_RETRIES:
            retries = IMAGE_WAIT_RETRIES
            logger.warning(
                "IMAGE_WAIT_RETRIES isn't a whole number from 0 to %d; using %d.",
                MAX_IMAGE_WAIT_RETRIES,
                retries,
            )
    interval = IMAGE_WAIT_INTERVAL
    value = os.getenv("IMAGE_WAIT_INTERVAL", "").strip()
    if value:
        try:
            interval = float(value)
        except ValueError:
            interval = -1.0
        if not (math.isfinite(interval) and 0 < interval <= MAX_IMAGE_WAIT_INTERVAL):
            interval = IMAGE_WAIT_INTERVAL
            logger.warning(
                "IMAGE_WAIT_INTERVAL isn't a number of seconds above 0 and up to "
                "%g; using %g.",
                MAX_IMAGE_WAIT_INTERVAL,
                interval,
            )
    return retries, interval


def _pause(
    seconds: float, delivery: _Delivery, deployment_name: str, image_repo: str
) -> bool:
    """Wait between checks, while a delivery is current.

    Sleeps in steps of at most `PAUSE_STEP` seconds, so that a delivery that
    a newer notification replaced stops soon and frees its worker.

    Args:
        seconds: Seconds to wait.
        delivery: Record of the delivery.
        deployment_name: Name of the deployment.
        image_repo: Canonical repository of the image.

    Returns:
        True after the whole wait, False as soon as the delivery was replaced.
    """
    while seconds > 0:
        step = min(seconds, PAUSE_STEP)
        time.sleep(step)
        seconds -= step
        if not _is_current(delivery, deployment_name, image_repo):
            return False
    return True


def _wait_for_image(
    image_repo: str,
    image_tag: str,
    delivery: _Delivery,
    deployment_name: str,
) -> bool:
    """Wait until an image is available, while a delivery is current.

    Checks the image up to `IMAGE_WAIT_RETRIES` + 1 times, `IMAGE_WAIT_INTERVAL`
    seconds apart (see `_image_wait`), and logs each failed check with its
    reason. After an answer with `Retry-After` (429 or 503) that asks for
    longer, the next check waits that long instead. The waits add up to at
    most retries times interval: a wait that wouldn't fit ends the wait at
    once.

    Args:
        image_repo: Canonical repository of the image.
        image_tag: Tag of the image.
        delivery: Record of the delivery.
        deployment_name: Name of the deployment.

    Returns:
        True once the image is available, False if the delivery was replaced.

    Raises:
        ImageUnavailable: If the image is still missing after all tries, with
            the reason of the last check.
    """
    retries, interval = _image_wait()
    remaining = retries * interval
    # Tolerance of the float sums, so that fractional intervals keep all checks
    slack = 1e-6
    reason: str | None = None
    for attempt in range(retries + 1):
        if not _is_current(delivery, deployment_name, image_repo):
            return False
        result = check_image(image_repo, image_tag)
        if result.available:
            return _is_current(delivery, deployment_name, image_repo)
        reason = result.reason or "unknown reason"
        logger.warning(
            "Check %d of %d for image %s:%s failed: %s",
            attempt + 1,
            retries + 1,
            image_repo,
            image_tag,
            reason,
        )
        if attempt == retries:
            break
        retry_after = result.retry_after
        if retry_after is not None and retry_after > interval:
            if retry_after > remaining + slack:
                reason += (
                    f"; it asks to wait {math.ceil(retry_after)} s, "
                    f"longer than the remaining wait of {math.ceil(remaining)} s"
                )
                break
            delay = retry_after
        else:
            delay = interval
            if delay > remaining + slack:
                reason += (
                    f"; the next check, {delay:g} s later, wouldn't fit in the "
                    f"remaining wait of {math.ceil(remaining)} s"
                )
                break
        if not _pause(delay, delivery, deployment_name, image_repo):
            return False
        remaining -= delay
    raise ImageUnavailable(reason)


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
            logger.warning(  # nosemgrep
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


class _TokenServiceError(Exception):
    """Raised when a registry's token service refuses or fails to give a token.

    Attributes:
        check: Outcome of the image check, with the token service's reason.
    """

    def __init__(self, check: ImageCheck) -> None:
        """Record the outcome.

        Args:
            check: Outcome of the image check.
        """
        super().__init__(check.reason)
        self.check = check


def _bearer_token(fields: dict[str, str], host: str, scheme: str) -> str | None:
    """Request a token from the token service that a registry names.

    Only token services over HTTPS are asked, as with Docker's own client,
    or an insecure registry itself, with the registry's credentials (see
    `_credentials`), if any. The response is read up to `MAX_TOKEN_RESPONSE`
    bytes (plus at most one chunk), for `REQUEST_TIMEOUT` seconds in all.

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
        _TokenServiceError: If the token service answers other than 200,
            without a token, or too much or too slowly.
    """
    realm = fields["realm"]
    # Over HTTP, only an insecure registry's own token service, as the token
    # (and credentials) would travel unencrypted
    if not (
        realm.startswith("https://")
        or (scheme == "http" and realm.startswith(f"http://{host}/"))
    ):
        return None
    deadline = time.monotonic() + REQUEST_TIMEOUT
    token_response = requests.get(
        realm,
        params={key: fields[key] for key in ("service", "scope") if key in fields},
        auth=_credentials(host),
        timeout=REQUEST_TIMEOUT,
        stream=True,
    )
    try:
        if token_response.status_code != 200:
            raise _TokenServiceError(_answer_reason(token_response, "token service"))
        content = b""
        for chunk in token_response.iter_content(chunk_size=TOKEN_CHUNK_SIZE):
            content += chunk
            # Also a service that trickles its response stops in time
            if len(content) > MAX_TOKEN_RESPONSE:
                raise _TokenServiceError(
                    ImageCheck(False, "token service answer too large")
                )
            if time.monotonic() > deadline:
                raise _TokenServiceError(ImageCheck(False, "token service too slow"))
    finally:
        token_response.close()
    body = json.loads(content)
    token = body.get("token") or body.get("access_token")
    if not (isinstance(token, str) and token):
        raise _TokenServiceError(ImageCheck(False, "token service answered no token"))
    return token


def _head_manifest(url: str, host: str, scheme: str) -> requests.Response:
    """Ask a registry for a manifest, and authenticate if it asks for it.

    Redirects are followed; `requests` drops the authorization on redirects
    to other hosts or from HTTPS to HTTP. Only challenges of the registry
    itself, with the scheme it was asked with, are answered, so that a host
    it redirects to can't ask for its credentials.

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
        _TokenServiceError: If the token service refuses or fails to give a
            token.
    """
    headers = {"Accept": MANIFEST_TYPES}
    response = requests.head(
        url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True
    )
    if response.status_code != 401:
        return response
    final = urlsplit(response.url) if response.history else None
    if final and (final.scheme, final.netloc) != (scheme, host):
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


def _raw_header(response: requests.Response, name: str) -> str | None:
    """Read a header of a registry's answer, whatever its value.

    Args:
        response: The registry's answer.
        name: Name of the header, in lower case.

    Returns:
        The value, stripped, or None if the header is missing.
    """
    for key, value in response.headers.items():
        if key.lower() == name:
            return str(value).strip()
    return None


def _header(response: requests.Response, name: str) -> str | None:
    """Read a header of a registry's answer for the reason of a failed check.

    Args:
        response: The registry's answer.
        name: Name of the header, in lower case.

    Returns:
        The value, or None if the header is missing or its value hasn't the
        header's syntax (see `QUOTED_HEADERS`).
    """
    value = _raw_header(response, name)
    return value if value and QUOTED_HEADERS[name].fullmatch(value) else None


def _x_header(response: requests.Response, name: str) -> str | None:
    """Read the older `X-` form of a rate-limit header, with its syntax.

    Args:
        response: The registry's answer.
        name: Name of the header without `x-`, in lower case.

    Returns:
        The value, or None if missing or not of the header's syntax.
    """
    value = _raw_header(response, f"x-{name}")
    return value if value and QUOTED_HEADERS[name].fullmatch(value) else None


def _retry_after(value: str | None) -> float | None:
    """Read the wait that `Retry-After` asks for.

    Args:
        value: Value of the header: seconds or an HTTP date.

    Returns:
        Seconds to wait, 0 for a date in the past, or None if the value is
        neither (or a date without a time zone: then UTC).
    """
    if value is None:
        return None
    if DELAY_SECONDS.fullmatch(value):
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    # Without a time zone (`-0000`), the date counts as UTC, as HTTP dates are
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, moment.timestamp() - time.time())


def _answer_reason(response: requests.Response, source: str = "registry") -> ImageCheck:
    """Explain an answer of a registry, or its token service, other than 200.

    Args:
        response: The answer.
        source: Who answered, for the reason.

    Returns:
        The outcome of the check, with the status, an explanation of it, and
        the registry's `Retry-After` and rate-limit headers, if any.
    """
    status = response.status_code
    details = []
    for name in QUOTED_HEADERS:
        value = _header(response, name)
        if value is None and name.startswith("ratelimit-"):
            value = _x_header(response, name)
        if value:
            details.append(f"{name}: {value}")
    parts = [part for part in (STATUS_HINTS.get(status), ", ".join(details)) if part]
    reason = f"{source} answered {status}"
    if parts:
        reason += f" ({'; '.join(parts)})"
    return ImageCheck(
        available=False,
        reason=reason,
        retry_after=(
            _retry_after(_raw_header(response, "retry-after"))
            if status in RETRY_STATUSES
            else None
        ),
    )


def check_image(image_repo: str, tag: str = "latest") -> ImageCheck:
    """Check whether the registry of an image has it, and if not, why not.

    Asks the registry for the image's manifest (OCI distribution API), over
    HTTPS, or HTTP for the hosts in `INSECURE_REGISTRIES`, with a token or
    Basic authentication if the registry asks for it (see `_head_manifest`).

    Args:
        image_repo: Canonical repository of the image, see `_parse_image`.
        tag: Tag of the image.

    Returns:
        The outcome. Network and authentication errors count as not
        available; the reason names the status or the class of the error,
        never its message, which could quote URLs.
    """
    host, name = _registry(image_repo)
    scheme = "http" if host in _insecure_registries() else "https"
    url = f"{scheme}://{host}/v2/{name}/manifests/{tag}"
    try:
        response = _head_manifest(url, host, scheme)
    except _TokenServiceError as exc:
        return exc.check
    except requests.RequestException as exc:
        return ImageCheck(
            available=False,
            reason=f"registry or token service not reachable ({type(exc).__name__})",
        )
    except (ValueError, AttributeError) as exc:
        return ImageCheck(
            available=False,
            reason=f"token service answered unusably ({type(exc).__name__})",
        )
    if response.status_code == 200:
        return ImageCheck(available=True)
    return _answer_reason(response)


def check_image_availability(image_repo: str, tag: str = "latest") -> bool:
    """Check whether the registry of an image has it.

    Args:
        image_repo: Canonical repository of the image, see `_parse_image`.
        tag: Tag of the image.

    Returns:
        Whether the image is available (see `check_image`, which also says
        why not).
    """
    return check_image(image_repo, tag).available
