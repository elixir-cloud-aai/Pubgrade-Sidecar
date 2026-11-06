"""Tests for the delivery of notifications and the rollout status."""

from collections.abc import Iterator
import threading
from typing import Any, Optional, cast
from unittest.mock import MagicMock, patch

from flask import Flask
from munch import DefaultMunch
import pytest
from werkzeug.exceptions import BadRequest, Unauthorized

from pubgrade_sidecar import controllers
from pubgrade_sidecar.errors.exceptions import ImageRepositoryMismatch

TOKEN = "test-token"
READ = "kubernetes.client.AppsV1Api.read_namespaced_deployment"
PATCH = "kubernetes.client.AppsV1Api.patch_namespaced_deployment"
CHECK = "pubgrade_sidecar.controllers.check_docker_image_availability"


class ImmediateExecutor:
    """Executor that runs submitted functions at once, in the caller."""

    def submit(self, function: Any, *args: Any) -> None:
        """Run a function.

        Args:
            function: Function to run.
            *args: Positional arguments of the function.
        """
        function(*args)


def deployment(
    image: str = "elixircloud/tesk-api:1.1.0",
    generation: int = 2,
    observed_generation: int = 2,
    replicas: int = 1,
    updated_replicas: int = 1,
    total_replicas: int = 1,
    available_replicas: int = 1,
    conditions: Optional[list[dict[str, str]]] = None,
    uid: str = "uid-1",
) -> DefaultMunch:
    """Build a Deployment as returned by the Kubernetes client.

    Args:
        image: Image of the first container.
        generation: Generation of the Deployment's metadata.
        observed_generation: Generation seen by the controller.
        replicas: Desired number of replicas.
        updated_replicas: Replicas with the current template.
        total_replicas: Existing replicas.
        available_replicas: Available replicas.
        conditions: Conditions of the Deployment.
        uid: Identifier of the Deployment.

    Returns:
        Object with the attributes used by the controllers.
    """
    return DefaultMunch.fromDict(
        {
            "metadata": {"generation": generation, "uid": uid},
            "spec": {
                "replicas": replicas,
                "template": {"spec": {"containers": [{"image": image}]}},
            },
            "status": {
                "observed_generation": observed_generation,
                "updated_replicas": updated_replicas,
                "replicas": total_replicas,
                "available_replicas": available_replicas,
                "conditions": conditions or [],
            },
        }
    )


@pytest.fixture(autouse=True)
def clean_deliveries() -> Iterator[None]:
    """Start and finish every test without recorded deliveries.

    Yields:
        Nothing.
    """
    controllers._deliveries.clear()
    yield
    controllers._deliveries.clear()


@pytest.fixture
def app() -> Flask:
    """Create an app.

    Returns:
        Flask app.
    """
    return Flask(__name__)


def notify(app: Flask, image_name: str, tag: str = "1.2.0") -> Any:
    """Send a notification.

    Args:
        app: Flask app.
        image_name: Image repository of the notification.
        tag: Tag of the notification.

    Returns:
        Return value of the controller.
    """
    with app.test_request_context(
        "url",
        headers={"X-Access-Token": TOKEN},
        json={"image_name": image_name, "tag": tag},
    ):
        return controllers.updateDeployment("tesk-api")


def status(app: Flask) -> dict[str, Any]:
    """Get the status of a deployment.

    Args:
        app: Flask app.

    Returns:
        Status of the deployment.
    """
    with app.test_request_context("url", headers={"X-Access-Token": TOKEN}):
        return cast(dict[str, Any], controllers.getImage("tesk-api").json)


@pytest.mark.parametrize(
    "image,expected",
    [
        ("elixircloud/tesk-api:1.1.0", ("elixircloud/tesk-api", "1.1.0")),
        ("elixircloud/tesk-api", ("elixircloud/tesk-api", None)),
        ("nginx:1", ("library/nginx", "1")),
        ("docker.io/nginx:1", ("library/nginx", "1")),
        ("docker.io/library/nginx:1", ("library/nginx", "1")),
        ("docker.io/elixircloud/tesk-api:1", ("elixircloud/tesk-api", "1")),
        ("index.docker.io/elixircloud/tesk-api:1", ("elixircloud/tesk-api", "1")),
        ("quay.io/org/app:1", ("quay.io/org/app", "1")),
        ("registry:5000/org/app:1", ("registry:5000/org/app", "1")),
        ("registry:5000/org/app", ("registry:5000/org/app", None)),
        ("org/app:1@sha256:abc", ("org/app", "1")),
        ("org/app@sha256:abc", ("org/app", None)),
    ],
)
def test_parse_image(image: str, expected: Any) -> None:
    """Test that images are split into a normalized repository and a tag."""
    assert controllers._parse_image(image) == expected


@pytest.mark.parametrize("token", [None, "wrong", "wröng"])
def test_token_rejected(app: Flask, token: Optional[str]) -> None:
    """Test that a missing or wrong token gets a 401, not a 500."""
    headers = {} if token is None else {"X-Access-Token": token.encode("utf-8")}
    with (
        app.test_request_context("url", headers=headers),
        pytest.raises(Unauthorized),
    ):
        controllers.getImage("tesk-api")


@patch(PATCH)
@patch(CHECK, return_value=True)
@patch(READ, return_value=deployment(image="other/image:1.0"))
def test_notify_other_repository(
    mock_read: MagicMock,
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
) -> None:
    """Test that a notification for another repository changes nothing."""
    with pytest.raises(ImageRepositoryMismatch):
        notify(app, "elixircloud/tesk-api")
    mock_check.assert_not_called()
    mock_patch.assert_not_called()


@pytest.mark.parametrize("tag", ["", "a/b", "1@sha256:abc", "a b", "-a", "a" * 129])
@patch(PATCH)
@patch(READ, return_value=deployment())
def test_notify_invalid_tag(
    mock_read: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    tag: str,
) -> None:
    """Test that a tag that isn't one is rejected."""
    with pytest.raises(BadRequest):
        notify(app, "elixircloud/tesk-api", tag=tag)
    mock_patch.assert_not_called()


@pytest.mark.parametrize(
    "current,notified,expected",
    [
        (
            "elixircloud/tesk-api:1.1.0",
            "elixircloud/tesk-api",
            "elixircloud/tesk-api:1.2.0",
        ),
        (
            "docker.io/elixircloud/tesk-api:1.1.0",
            "elixircloud/tesk-api",
            "docker.io/elixircloud/tesk-api:1.2.0",
        ),
        (
            "elixircloud/tesk-api:1.1.0",
            "docker.io/elixircloud/tesk-api",
            "elixircloud/tesk-api:1.2.0",
        ),
    ],
)
@patch(PATCH)
@patch(CHECK, return_value=True)
@patch(READ)
def test_notify_patches_tag(
    mock_read: MagicMock,
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    current: str,
    notified: str,
    expected: str,
) -> None:
    """Test that only the tag changes, whatever spelling is used."""
    monkeypatch.setattr(controllers, "_executor", ImmediateExecutor())
    mock_read.return_value = deployment(image=current)
    response, code = notify(app, notified)
    assert code == 202
    assert response.json["image"] == expected
    mock_check.assert_called_once_with("elixircloud/tesk-api", "1.2.0")
    body = mock_patch.call_args.kwargs["body"]
    assert body[0]["value"] == expected


@patch(PATCH)
@patch(READ, return_value=deployment())
def test_notify_returns_at_once(
    mock_read: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
) -> None:
    """Test that a notification doesn't wait for the image or the rollout."""
    release = threading.Event()
    patched = threading.Event()

    def finish_patch(**_: Any) -> DefaultMunch:
        patched.set()
        return deployment()

    mock_patch.side_effect = finish_patch

    def wait_for_release(image_name: str, tag: str) -> bool:
        release.wait(timeout=10)
        return True

    with patch(CHECK, side_effect=wait_for_release):
        _, code = notify(app, "elixircloud/tesk-api")
        assert code == 202
        mock_patch.assert_not_called()
        assert status(app)["status"] == "waiting_for_image"
        release.set()
        assert patched.wait(timeout=10)
    mock_patch.assert_called_once()


@patch(PATCH)
@patch("pubgrade_sidecar.controllers.time.sleep")
@patch(CHECK, return_value=False)
@patch(READ, return_value=deployment())
def test_image_never_available(
    mock_read: MagicMock,
    mock_check: MagicMock,
    mock_sleep: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a missing image is reported, and the deployment untouched."""
    monkeypatch.setattr(controllers, "_executor", ImmediateExecutor())
    notify(app, "elixircloud/tesk-api")
    assert mock_check.call_count == controllers.MAX_RETRY + 1
    assert mock_sleep.call_count == controllers.MAX_RETRY
    mock_patch.assert_not_called()
    result = status(app)
    assert result["status"] == "failed"
    assert result["requested_image"] == "elixircloud/tesk-api:1.2.0"
    assert "Image not available" in result["detail"]


@patch(PATCH, side_effect=RuntimeError("secret detail"))
@patch(CHECK, return_value=True)
@patch(READ, return_value=deployment())
def test_patch_fails(
    mock_read: MagicMock,
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a failed patch is reported, without internal details."""
    monkeypatch.setattr(controllers, "_executor", ImmediateExecutor())
    notify(app, "elixircloud/tesk-api")
    result = status(app)
    assert result["status"] == "failed"
    assert result["detail"] == "Deployment could not be updated."


@patch(PATCH)
@patch(READ, return_value=deployment())
def test_newer_notification_wins(
    mock_read: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a delivery that was superseded doesn't patch."""
    pending = []

    class DeferredExecutor:
        """Executor that runs submitted functions on request."""

        def submit(self, function: Any, *args: Any) -> None:
            """Keep a function for later.

            Args:
                function: Function to run later.
                *args: Positional arguments of the function.
            """
            pending.append((function, args))

    monkeypatch.setattr(controllers, "_executor", DeferredExecutor())
    notify(app, "elixircloud/tesk-api", tag="1.2.0")
    notify(app, "elixircloud/tesk-api", tag="1.3.0")
    with patch(CHECK, return_value=True):
        for function, args in pending:
            function(*args)
    mock_patch.assert_called_once()
    body = mock_patch.call_args.kwargs["body"]
    assert body[0]["value"] == "elixircloud/tesk-api:1.3.0"


@pytest.mark.parametrize(
    "state,expected",
    [
        ({}, "complete"),
        ({"observed_generation": 1}, "rolling_out"),
        ({"updated_replicas": 0}, "rolling_out"),
        ({"updated_replicas": None}, "rolling_out"),
        ({"total_replicas": 2}, "rolling_out"),
        ({"available_replicas": 0}, "rolling_out"),
        ({"available_replicas": None}, "rolling_out"),
        (
            {
                "updated_replicas": 0,
                "conditions": [
                    {"type": "Progressing", "reason": "ProgressDeadlineExceeded"}
                ],
            },
            "failed",
        ),
        (
            {"conditions": [{"type": "Available", "reason": "MinimumReplicas"}]},
            "complete",
        ),
        (
            {
                "observed_generation": 1,
                "conditions": [
                    {"type": "Progressing", "reason": "ProgressDeadlineExceeded"}
                ],
            },
            "rolling_out",
        ),
    ],
)
@patch(READ)
def test_rollout_status(
    mock_read: MagicMock,
    app: Flask,
    state: dict[str, Any],
    expected: str,
) -> None:
    """Test that the rollout status follows the state of the Deployment."""
    mock_read.return_value = deployment(**state)
    result = status(app)
    assert result["status"] == expected
    assert result["image"] == "elixircloud/tesk-api:1.1.0"
    assert result["replicas"] == 1


@patch(PATCH)
@patch(CHECK, return_value=True)
@patch(READ, return_value=deployment())
def test_status_after_patch_follows_deployment(
    mock_read: MagicMock,
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that after a patch, the Deployment decides the status."""
    monkeypatch.setattr(controllers, "_executor", ImmediateExecutor())
    mock_patch.return_value = deployment(generation=3)
    notify(app, "elixircloud/tesk-api")
    # Patched, but the read Deployment is older: not done, though it looks healthy
    mock_read.return_value = deployment()
    assert status(app)["status"] == "rolling_out"
    new = "elixircloud/tesk-api:1.2.0"
    mock_read.return_value = deployment(image=new, generation=3, observed_generation=2)
    assert status(app)["status"] == "rolling_out"
    mock_read.return_value = deployment(image=new, generation=3, observed_generation=3)
    result = status(app)
    assert result["status"] == "complete"
    assert result["requested_image"] == new
    assert "detail" not in result
    # A rollback by hand: a poller must not wait for ever
    mock_read.return_value = deployment(generation=5, observed_generation=5)
    assert status(app)["status"] == "complete"
    # The same for a Deployment that was recreated, with a new generation count
    mock_read.return_value = deployment(
        generation=1, observed_generation=1, uid="uid-2"
    )
    assert status(app)["status"] == "complete"


@pytest.mark.parametrize(
    "current,tag",
    [
        ("elixircloud/tesk-api", "latest"),
        ("elixircloud/tesk-api:1.2.0@sha256:abc", "1.2.0"),
    ],
)
@patch(PATCH)
@patch(READ)
def test_notify_same_effective_tag(
    mock_read: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    current: str,
    tag: str,
) -> None:
    """Test that untagged images and digests are no reason for a rollout."""
    mock_read.return_value = deployment(image=current)
    response, code = notify(app, "elixircloud/tesk-api", tag=tag)
    assert code == 200
    assert response.json["image"] == current
    mock_patch.assert_not_called()


@patch(READ, side_effect=RuntimeError("no cluster"))
def test_notify_invalid_tag_needs_no_cluster(mock_read: MagicMock, app: Flask) -> None:
    """Test that an invalid tag is rejected before Kubernetes is asked."""
    with pytest.raises(BadRequest):
        notify(app, "elixircloud/tesk-api", tag="a/b")
    mock_read.assert_not_called()


@patch(PATCH)
@patch(CHECK, return_value=False)
@patch(READ, return_value=deployment(image="elixircloud/tesk-api:1.2.0"))
def test_notify_current_image(
    mock_read: MagicMock,
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that the image in use leads to no rollout, and replaces older ones."""
    monkeypatch.setattr(controllers, "_executor", ImmediateExecutor())
    controllers._deliveries["tesk-api"] = controllers._Delivery(image="older")
    response, code = notify(app, "elixircloud/tesk-api", tag="1.2.0")
    assert code == 200
    assert "already" in response.json["message"]
    mock_check.assert_not_called()
    mock_patch.assert_not_called()
    assert "requested_image" not in status(app)


@patch(PATCH, side_effect=RuntimeError("timed out"))
@patch(CHECK, return_value=True)
@patch(READ, return_value=deployment())
def test_failed_delivery_ends_with_the_requested_image(
    mock_read: MagicMock,
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a failure no longer counts once the Deployment has the image.

    E.g., after an answer lost in a timeout, or an upgrade by hand.
    """
    monkeypatch.setattr(controllers, "_executor", ImmediateExecutor())
    notify(app, "elixircloud/tesk-api")
    assert status(app)["status"] == "failed"
    mock_read.return_value = deployment(image="elixircloud/tesk-api:1.2.0")
    result = status(app)
    assert result["status"] == "complete"
    assert "detail" not in result


@patch(PATCH)
@patch("pubgrade_sidecar.controllers.time.sleep")
@patch(CHECK, return_value=False)
@patch(READ, return_value=deployment())
def test_superseded_delivery_stops_waiting(
    mock_read: MagicMock,
    mock_check: MagicMock,
    mock_sleep: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a replaced delivery gives up its worker."""
    calls = []

    def replace_delivery(image_name: str, tag: str) -> bool:
        calls.append(tag)
        controllers._deliveries["tesk-api"] = controllers._Delivery(image="other")
        return False

    mock_check.side_effect = replace_delivery
    monkeypatch.setattr(controllers, "_executor", ImmediateExecutor())
    notify(app, "elixircloud/tesk-api")
    assert calls == ["1.2.0"]
    mock_patch.assert_not_called()


@patch("pubgrade_sidecar.controllers.requests.get")
def test_check_image_uses_timeout(mock_get: MagicMock) -> None:
    """Test that the check for the image can't block for ever."""
    mock_get.return_value.status_code = 200
    assert controllers.check_docker_image_availability("org/app", "1")
    assert mock_get.call_args.kwargs["timeout"] > 0


@patch(
    "pubgrade_sidecar.controllers.requests.get",
    side_effect=controllers.requests.ConnectionError,
)
def test_check_image_network_error(mock_get: MagicMock) -> None:
    """Test that a network error counts as image not available yet."""
    assert not controllers.check_docker_image_availability("org/app", "1")
