"""Tests for updating every container of an image repository."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from flask import Flask
from kubernetes.client.rest import ApiException
from munch import DefaultMunch
import pytest
from werkzeug.exceptions import BadRequest, Forbidden, InternalServerError, NotFound

from pubgrade_sidecar import controllers
from pubgrade_sidecar.errors.exceptions import ImageRepositoryMismatch

TOKEN = "test-token"
READ = "kubernetes.client.AppsV1Api.read_namespaced_deployment"
LIST = "kubernetes.client.AppsV1Api.list_namespaced_deployment"
PATCH = "kubernetes.client.AppsV1Api.patch_namespaced_deployment"
CHECK = "pubgrade_sidecar.controllers.check_image_availability"
REPOSITORY = "elixircloud/tesk-api"


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
    name: str, images: list[str], labels: dict[str, str] | None = None
) -> DefaultMunch:
    """Build a Deployment as returned by the Kubernetes client.

    Args:
        name: Name of the Deployment.
        images: Images of its containers, in order.
        labels: Labels of the Deployment.

    Returns:
        Object with the attributes used by the controllers.
    """
    return DefaultMunch.fromDict(
        {
            "metadata": {
                "name": name,
                "generation": 2,
                "uid": f"uid-{name}",
                "labels": labels,
            },
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {"containers": [{"image": image} for image in images]}
                },
            },
            "status": {
                "observed_generation": 2,
                "updated_replicas": 1,
                "replicas": 1,
                "available_replicas": 1,
            },
        }
    )


DEPLOYMENTS = {
    # Two containers of the repository, in two spellings, and another one
    "tesk-api": deployment(
        "tesk-api",
        ["other/proxy:1.0", f"{REPOSITORY}:1.1.0", f"docker.io/{REPOSITORY}:1.1.0"],
        {"tier": "api"},
    ),
    "tesk-worker": deployment("tesk-worker", [f"{REPOSITORY}:1.1.0"]),
    "mongodb": deployment("mongodb", ["mongo:7.0"], {"tier": "api"}),
}


def read(_: Any, name: str, namespace: str, **kwargs: Any) -> DefaultMunch:
    """Read a Deployment of `DEPLOYMENTS`, as the Kubernetes client does.

    Args:
        name: Name of the Deployment.
        namespace: Namespace of the Deployment.
        **kwargs: Further arguments of the call.

    Returns:
        The Deployment.

    Raises:
        ApiException: With status 404, if there is no such Deployment.
    """
    try:
        return DEPLOYMENTS[name]
    except KeyError:
        raise ApiException(status=404) from None


@pytest.fixture(autouse=True)
def clean_deliveries(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run deliveries at once, without records or settings of other tests.

    Args:
        monkeypatch: Fixture for changing the environment.

    Yields:
        Nothing.
    """
    monkeypatch.setattr(controllers, "_executor", ImmediateExecutor())
    monkeypatch.setenv("TARGET_DEPLOYMENTS", "tesk-api, tesk-worker,mongodb")
    monkeypatch.delenv("DEPLOYMENT_LABEL", raising=False)
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


def notify_all(app: Flask, image_name: str = REPOSITORY, tag: str = "1.2.0") -> Any:
    """Send a notification for all deployments.

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
        return controllers.updateDeployments()


def notify_one(
    app: Flask, name: str, image_name: str = REPOSITORY, tag: str = "1.2.0"
) -> Any:
    """Send a notification for one deployment.

    Args:
        app: Flask app.
        name: Name of the deployment.
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
        return controllers.updateDeployment(name)


def patches(mock_patch: MagicMock) -> dict[str, list[dict[str, str]]]:
    """Collect the patches sent to the Kubernetes API.

    Args:
        mock_patch: Mock of the patch call.

    Returns:
        Patch by name of the deployment.
    """
    return {call.kwargs["name"]: call.kwargs["body"] for call in mock_patch.mock_calls}


def replace(index: int, old: str, new: str) -> list[dict[str, str]]:
    """Build the patch operations for the image of one container.

    Args:
        index: Index of the container.
        old: Image that the container must still have.
        new: New image.

    Returns:
        The operations: a test of the current image, then its replacement.
    """
    path = f"/spec/template/spec/containers/{index}/image"
    return [
        {"op": "test", "value": old, "path": path},
        {"op": "replace", "value": new, "path": path},
    ]


@patch(PATCH, side_effect=lambda **kwargs: DEPLOYMENTS[kwargs["name"]])
@patch(CHECK, return_value=True)
@patch(READ, new=read)
def test_one_deployment_all_containers(
    mock_check: MagicMock, mock_patch: MagicMock, app: Flask
) -> None:
    """Test that every container of the repository changes, in its spelling."""
    response, code = notify_one(app, "tesk-api")
    assert code == 202
    assert response.json["image"] == f"{REPOSITORY}:1.2.0"
    assert patches(mock_patch) == {
        "tesk-api": [
            *replace(1, f"{REPOSITORY}:1.1.0", f"{REPOSITORY}:1.2.0"),
            *replace(
                2, f"docker.io/{REPOSITORY}:1.1.0", f"docker.io/{REPOSITORY}:1.2.0"
            ),
        ]
    }


@patch(PATCH)
@patch(READ, new=read)
def test_one_deployment_without_the_repository(
    mock_patch: MagicMock, app: Flask
) -> None:
    """Test that a deployment without the repository changes nothing."""
    with pytest.raises(ImageRepositoryMismatch):
        notify_one(app, "mongodb")
    mock_patch.assert_not_called()


@patch(PATCH, side_effect=lambda **kwargs: DEPLOYMENTS[kwargs["name"]])
@patch(CHECK, return_value=True)
@patch(READ, new=read)
def test_all_deployments(
    mock_check: MagicMock, mock_patch: MagicMock, app: Flask
) -> None:
    """Test that every deployment with the repository is updated."""
    response, code = notify_all(app)
    assert code == 202
    assert response.json["deployments"] == [
        {"name": "tesk-api", "image": f"{REPOSITORY}:1.2.0"},
        {"name": "tesk-worker", "image": f"{REPOSITORY}:1.2.0"},
    ]
    assert patches(mock_patch) == {
        "tesk-api": [
            *replace(1, f"{REPOSITORY}:1.1.0", f"{REPOSITORY}:1.2.0"),
            *replace(
                2, f"docker.io/{REPOSITORY}:1.1.0", f"docker.io/{REPOSITORY}:1.2.0"
            ),
        ],
        "tesk-worker": replace(0, f"{REPOSITORY}:1.1.0", f"{REPOSITORY}:1.2.0"),
    }
    with app.test_request_context("url", headers={"X-Access-Token": TOKEN}):
        report = cast(dict[str, Any], controllers.getImage("tesk-worker").json)
    assert report["requested_image"] == f"{REPOSITORY}:1.2.0"


@pytest.mark.parametrize("label", ["tier=api", "tier", " tier = api "])
@patch(PATCH, side_effect=lambda **kwargs: DEPLOYMENTS[kwargs["name"]])
@patch(CHECK, return_value=True)
@patch(READ, new=read)
def test_all_deployments_with_label(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> None:
    """Test that only deployments with the label are updated."""
    monkeypatch.setenv("DEPLOYMENT_LABEL", label)
    _, code = notify_all(app)
    assert code == 202
    assert list(patches(mock_patch)) == ["tesk-api"]


@patch(PATCH)
@patch(READ, new=read)
def test_all_deployments_label_value_differs(
    mock_patch: MagicMock, app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that a label with another value doesn't match."""
    monkeypatch.setenv("DEPLOYMENT_LABEL", "tier=web")
    with pytest.raises(NotFound):
        notify_all(app)
    mock_patch.assert_not_called()


@patch(PATCH)
@patch(READ, new=read)
def test_all_deployments_none_matches(mock_patch: MagicMock, app: Flask) -> None:
    """Test that a repository that no container uses gets a 404."""
    with pytest.raises(NotFound):
        notify_all(app, image_name="elixircloud/other")
    mock_patch.assert_not_called()


@patch(PATCH)
@patch(READ, new=read)
def test_all_deployments_current(
    mock_patch: MagicMock, app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that deployments that use the tag already aren't patched."""
    monkeypatch.setenv("TARGET_DEPLOYMENTS", "tesk-worker")
    response, code = notify_all(app, tag="1.1.0")
    assert code == 200
    assert response.json["deployments"] == [
        {"name": "tesk-worker", "image": f"{REPOSITORY}:1.1.0"}
    ]
    mock_patch.assert_not_called()


@patch(PATCH, side_effect=lambda **kwargs: DEPLOYMENTS[kwargs["name"]])
@patch(CHECK, return_value=True)
@patch(READ, new=read)
def test_all_deployments_skips_missing(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a listed deployment that doesn't exist is skipped."""
    monkeypatch.setenv("TARGET_DEPLOYMENTS", "gone,tesk-worker")
    _, code = notify_all(app)
    assert code == 202
    assert list(patches(mock_patch)) == ["tesk-worker"]


@pytest.mark.parametrize("status,error", [(403, Forbidden), (503, InternalServerError)])
@patch(PATCH)
def test_all_deployments_read_fails(
    mock_patch: MagicMock, app: Flask, status: int, error: type[Exception]
) -> None:
    """Test that other errors of the Kubernetes API stop the notification."""
    with (
        patch(READ, side_effect=ApiException(status=status)),
        pytest.raises(error),
    ):
        notify_all(app)
    mock_patch.assert_not_called()


@patch(PATCH, side_effect=lambda **kwargs: DEPLOYMENTS[kwargs["name"]])
@patch(CHECK, return_value=True)
@patch(LIST)
@patch(READ, new=read)
def test_all_deployments_listed_without_targets(
    mock_list: MagicMock,
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that without target deployments, all in the namespace count."""
    monkeypatch.delenv("TARGET_DEPLOYMENTS")
    mock_list.return_value = SimpleNamespace(
        items=[DEPLOYMENTS["mongodb"], DEPLOYMENTS["tesk-worker"]]
    )
    _, code = notify_all(app)
    assert code == 202
    assert list(patches(mock_patch)) == ["tesk-worker"]


@patch(LIST, side_effect=ApiException(status=403))
def test_all_deployments_list_fails(
    mock_list: MagicMock, app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that a refused listing is a 403."""
    monkeypatch.delenv("TARGET_DEPLOYMENTS")
    with pytest.raises(Forbidden):
        notify_all(app)


@patch(READ)
def test_all_deployments_invalid_tag(mock_read: MagicMock, app: Flask) -> None:
    """Test that an invalid tag needs no cluster."""
    with pytest.raises(BadRequest):
        notify_all(app, tag="a/b")
    mock_read.assert_not_called()


@patch(PATCH, side_effect=lambda **kwargs: DEPLOYMENTS[kwargs["name"]])
@patch(CHECK, return_value=True)
@patch(READ, new=read)
def test_one_deployment_other_registry(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that images of other registries are rolled out too."""
    image = "harbor.example.org:8443/project/app"
    monkeypatch.setenv("TARGET_DEPLOYMENTS", "harbor-app")
    monkeypatch.setitem(
        DEPLOYMENTS, "harbor-app", deployment("harbor-app", [f"{image}:1.0"])
    )
    response, code = notify_one(app, "harbor-app", image_name=image)
    assert code == 202
    assert response.json["image"] == f"{image}:1.2.0"
    mock_check.assert_called_once_with(image, "1.2.0")


class DeferredExecutor:
    """Executor that keeps submitted functions until they are run."""

    def __init__(self) -> None:
        """Start without functions."""
        self.pending: list[tuple[Any, tuple[Any, ...]]] = []

    def submit(self, function: Any, *args: Any) -> None:
        """Keep a function.

        Args:
            function: Function to run.
            *args: Positional arguments of the function.
        """
        self.pending.append((function, args))

    def run(self) -> None:
        """Run the kept functions, in order."""
        for function, args in self.pending:
            function(*args)


def status_of(app: Flask, name: str) -> dict[str, Any]:
    """Get the status report of a deployment.

    Args:
        app: Flask app.
        name: Name of the deployment.

    Returns:
        The report.
    """
    with app.test_request_context("url", headers={"X-Access-Token": TOKEN}):
        return cast(dict[str, Any], controllers.getImage(name).json)


@patch(PATCH, side_effect=lambda **kwargs: DEPLOYMENTS[kwargs["name"]])
@patch(CHECK, return_value=True)
@patch(READ, new=read)
def test_repositories_of_one_deployment_dont_replace_each_other(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a notification for another repository keeps a pending one."""
    executor = DeferredExecutor()
    monkeypatch.setattr(controllers, "_executor", executor)
    assert notify_one(app, "tesk-api")[1] == 202
    # A notification for an image in use ends no delivery of another repository
    assert notify_one(app, "tesk-api", image_name="other/proxy", tag="1.0")[1] == 200
    assert notify_one(app, "tesk-api", image_name="other/proxy")[1] == 202
    executor.run()
    applied = [
        operation["value"]
        for call in mock_patch.mock_calls
        for operation in call.kwargs["body"]
        if operation["op"] == "replace"
    ]
    assert applied == [
        f"{REPOSITORY}:1.2.0",
        f"docker.io/{REPOSITORY}:1.2.0",
        "other/proxy:1.2.0",
    ]


@patch(PATCH)
@patch(CHECK, return_value=False)
@patch("pubgrade_sidecar.controllers.time.sleep")
@patch(READ)
def test_status_needs_every_target_container(
    mock_read: MagicMock,
    mock_sleep: MagicMock,
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
) -> None:
    """Test that a failure counts while one target container lacks the image."""
    mock_read.return_value = deployment(
        "tesk-api", [f"{REPOSITORY}:1.2.0", f"docker.io/{REPOSITORY}:1.1.0"]
    )
    _, code = notify_one(app, "tesk-api")
    assert code == 202
    report = status_of(app, "tesk-api")
    assert report["status"] == "failed"
    assert report["detail"]
    mock_patch.assert_not_called()


@patch(PATCH)
@patch(READ, new=read)
def test_status_reports_failure_of_any_repository(
    mock_patch: MagicMock, app: Flask
) -> None:
    """Test that a failed delivery counts over a newer waiting or patched one."""
    failed = controllers._Delivery(
        image=f"{REPOSITORY}:1.2.0",
        state="failed",
        detail="Image not available.",
        targets=[(1, f"{REPOSITORY}:1.2.0")],
    )
    waiting = controllers._Delivery(
        image="other/proxy:1.1.0", targets=[(0, "other/proxy:1.1.0")]
    )
    # Patched, and the Deployment has moved past it
    patched = controllers._Delivery(
        image="mongo:7.0", state="patched", generation=1, uid="uid-tesk-api"
    )
    controllers._deliveries[("tesk-api", REPOSITORY)] = failed
    controllers._deliveries[("tesk-api", "other/proxy")] = waiting
    controllers._deliveries[("tesk-api", "library/mongo")] = patched
    report = status_of(app, "tesk-api")
    assert report["status"] == "failed"
    assert report["requested_image"] == f"{REPOSITORY}:1.2.0"
    assert report["detail"] == "Image not available."
    del controllers._deliveries[("tesk-api", REPOSITORY)]
    report = status_of(app, "tesk-api")
    assert report["status"] == "waiting_for_image"
    assert report["requested_image"] == "other/proxy:1.1.0"
    assert "detail" not in report
    del controllers._deliveries[("tesk-api", "other/proxy")]
    patched.generation = 3
    report = status_of(app, "tesk-api")
    assert report["status"] == "rolling_out"
    assert report["requested_image"] == "mongo:7.0"


@patch(PATCH, side_effect=ApiException(status=422))
@patch(CHECK, return_value=True)
@patch(READ, new=read)
def test_changed_container_fails_the_delivery(
    mock_check: MagicMock, mock_patch: MagicMock, app: Flask
) -> None:
    """Test that a patch whose test fails is reported as a failed delivery."""
    _, code = notify_one(app, "tesk-api")
    assert code == 202
    report = status_of(app, "tesk-api")
    assert report["status"] == "failed"
    assert report["detail"] == "Deployment could not be updated."


@pytest.mark.parametrize(
    "now,expected",
    [
        # Patched by an older delivery meanwhile
        (
            [f"{REPOSITORY}:1.1.0", f"{REPOSITORY}:1.0.0"],
            [
                *replace(0, f"{REPOSITORY}:1.1.0", f"{REPOSITORY}:1.2.0"),
                *replace(1, f"{REPOSITORY}:1.0.0", f"{REPOSITORY}:1.2.0"),
            ],
        ),
        # Reordered, with a container added
        (
            ["other/proxy:1.0", f"{REPOSITORY}:1.0.0", f"docker.io/{REPOSITORY}:1.0.0"],
            [
                *replace(1, f"{REPOSITORY}:1.0.0", f"{REPOSITORY}:1.2.0"),
                *replace(
                    2, f"docker.io/{REPOSITORY}:1.0.0", f"docker.io/{REPOSITORY}:1.2.0"
                ),
            ],
        ),
    ],
)
@patch(PATCH, side_effect=lambda **kwargs: DEPLOYMENTS["tesk-worker"])
@patch(CHECK, return_value=True)
@patch(READ)
def test_patch_planned_from_current_deployment(
    mock_read: MagicMock,
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    now: list[str],
    expected: list[dict[str, str]],
) -> None:
    """Test that the patch follows changes of the deployment during the wait."""
    executor = DeferredExecutor()
    monkeypatch.setattr(controllers, "_executor", executor)
    mock_read.return_value = deployment(
        "tesk-worker", [f"{REPOSITORY}:1.0.0", f"{REPOSITORY}:1.0.0"]
    )
    assert notify_one(app, "tesk-worker")[1] == 202
    mock_read.return_value = deployment("tesk-worker", now)
    executor.run()
    assert mock_patch.call_args.kwargs["body"] == expected
    delivery = controllers._deliveries[("tesk-worker", REPOSITORY)]
    assert delivery.state == "patched"
    assert delivery.targets == [
        (int(operation["path"].split("/")[-2]), operation["value"])
        for operation in expected
        if operation["op"] == "replace"
    ]


@pytest.mark.parametrize(
    "now,state",
    [
        # Updated by hand meanwhile
        ([f"{REPOSITORY}:1.2.0"], "patched"),
        # No longer uses the repository
        (["other/proxy:1.0"], "failed"),
    ],
)
@patch(PATCH)
@patch(CHECK, return_value=True)
@patch(READ)
def test_nothing_to_patch_after_the_wait(
    mock_read: MagicMock,
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    now: list[str],
    state: str,
) -> None:
    """Test that a deployment changed during the wait isn't patched."""
    executor = DeferredExecutor()
    monkeypatch.setattr(controllers, "_executor", executor)
    mock_read.return_value = deployment("tesk-worker", [f"{REPOSITORY}:1.0.0"])
    assert notify_one(app, "tesk-worker")[1] == 202
    mock_read.return_value = deployment("tesk-worker", now)
    executor.run()
    mock_patch.assert_not_called()
    assert controllers._deliveries[("tesk-worker", REPOSITORY)].state == state


@pytest.mark.parametrize("label", ["=api", " = "])
@patch(PATCH)
@patch(READ, new=read)
def test_label_without_key_matches_none(
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> None:
    """Test that a label setting without a key limits to no deployment."""
    monkeypatch.setenv("DEPLOYMENT_LABEL", label)
    with pytest.raises(NotFound):
        notify_all(app)
    mock_patch.assert_not_called()


@patch(PATCH, side_effect=lambda **kwargs: DEPLOYMENTS[kwargs["name"]])
@patch(CHECK, return_value=True)
@patch(READ, new=read)
def test_all_deployments_lists_only_rollouts(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that 202 lists only the deployments that roll out."""
    monkeypatch.setitem(
        DEPLOYMENTS, "tesk-worker", deployment("tesk-worker", [f"{REPOSITORY}:1.2.0"])
    )
    response, code = notify_all(app)
    assert code == 202
    assert [item["name"] for item in response.json["deployments"]] == ["tesk-api"]
