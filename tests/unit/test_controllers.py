"""Tests for the controllers, called directly in a request context."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from flask import Flask
from munch import DefaultMunch
import pytest

from pubgrade_sidecar import controllers
from pubgrade_sidecar.controllers import (
    getDeployment,
    getImage,
    updateDeployment,
)
from pubgrade_sidecar.errors.exceptions import NotFound, Unauthorized


def mock_list_namespaced_deployment(
    self: Any, namespace: str, **kwargs: Any
) -> SimpleNamespace:
    """Return a list of deployments, as the Kubernetes client does.

    Args:
        self: The API client.
        namespace: Namespace of the deployments.
        **kwargs: Further arguments of the call.

    Returns:
        Object whose `items` are the deployments.
    """
    items = [
        {"metadata": {"name": "build-complete-updater"}},
        {"metadata": {"name": "mongodb"}},
        {"metadata": {"name": "pubgrade"}},
    ]
    return SimpleNamespace(items=DefaultMunch.fromDict(items))


def mock_read_namespaced_deployment(
    self: Any, deployment_name: str, namespace: str, **kwargs: Any
) -> DefaultMunch:
    """Return a deployment whose rollout is complete.

    Args:
        self: The API client.
        deployment_name: Name of the deployment.
        namespace: Namespace of the deployment.
        **kwargs: Further arguments of the call.

    Returns:
        The deployment.
    """
    resp = {
        "metadata": {"generation": 1},
        "status": {
            "observed_generation": 1,
            "updated_replicas": 1,
            "replicas": 1,
            "available_replicas": 1,
        },
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {"containers": [{"image": "akash7778/pubgrade:latest"}]}
            },
        },
    }
    return DefaultMunch.fromDict(resp)


def mock_read_namespaced_deployment_not_found(
    self: Any, deployment_name: str, namespace: str, **kwargs: Any
) -> DefaultMunch:
    """Return a deployment without containers.

    Args:
        self: The API client.
        deployment_name: Name of the deployment.
        namespace: Namespace of the deployment.
        **kwargs: Further arguments of the call.

    Returns:
        The deployment.
    """
    resp: dict[str, Any] = {"spec": {"template": {"spec": {"containers": []}}}}
    return DefaultMunch.fromDict(resp)


@patch(
    "kubernetes.client.AppsV1Api.list_namespaced_deployment",
    mock_list_namespaced_deployment,
)
def test_get_deployments() -> None:
    """Test that the names of the deployments are listed."""
    app = Flask(__name__)
    with app.test_request_context("url", headers={"X-Access-Token": "test-token"}):
        assert getDeployment() == ["build-complete-updater", "mongodb", "pubgrade"]


def test_get_deployments_unauthorized() -> None:
    """Test that a wrong token is rejected when listing deployments."""
    app = Flask(__name__)
    with (
        app.test_request_context(
            "url", headers={"X-Access-Token": "wrong_access_token"}
        ),
        pytest.raises(Unauthorized),
    ):
        getDeployment()


@pytest.mark.parametrize(
    "headers", [{}, {"X-Access-Token": ""}, {"X-Access-Token": "test-token"}]
)
@pytest.mark.parametrize(
    "call",
    [
        getDeployment,
        lambda: getImage("pubgrade"),
        lambda: updateDeployment("pubgrade"),
    ],
)
def test_no_token_configured(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    call: Callable[[], Any],
) -> None:
    """Test that all calls are rejected if no token is configured."""
    monkeypatch.delenv("ACCESS_TOKEN")
    app = Flask(__name__)
    with (
        app.test_request_context("url", headers=headers),
        pytest.raises(Unauthorized),
    ):
        call()


@patch("kubernetes.client.AppsV1Api.patch_namespaced_deployment")
@patch("kubernetes.client.AppsV1Api.read_namespaced_deployment")
def test_update_deployment_unauthorized(
    mock_read: MagicMock, mock_patch: MagicMock
) -> None:
    """Test that a wrong token is rejected before the cluster is called."""
    app = Flask(__name__)
    with (
        app.test_request_context(
            "url",
            headers={"X-Access-Token": "wrong_access_token"},
            json={"image_name": "akash7778/pubgrade", "tag": "1.0"},
        ),
        pytest.raises(Unauthorized),
    ):
        updateDeployment("pubgrade")
    mock_read.assert_not_called()
    mock_patch.assert_not_called()


@patch(
    "kubernetes.client.AppsV1Api.read_namespaced_deployment",
    mock_read_namespaced_deployment,
)
def test_get_image() -> None:
    """Test that the image of a deployment is reported."""
    app = Flask(__name__)
    with app.test_request_context("url", headers={"X-Access-Token": "test-token"}):
        assert (
            cast(dict[str, Any], getImage("pubgrade").json)["image"]
            == "akash7778/pubgrade:latest"
        )


def test_get_image_unauthorized() -> None:
    """Test that a wrong token is rejected when reading a deployment."""
    app = Flask(__name__)
    with (
        app.test_request_context(
            "url", headers={"X-Access-Token": "wrong_access_token"}
        ),
        pytest.raises(Unauthorized),
    ):
        getImage("pubgrade")


@patch(
    "kubernetes.client.AppsV1Api.read_namespaced_deployment",
    mock_read_namespaced_deployment_not_found,
)
def test_get_image_not_found() -> None:
    """Test that a deployment without containers counts as not found."""
    app = Flask(__name__)
    with (
        app.test_request_context("url", headers={"X-Access-Token": "test-token"}),
        pytest.raises(NotFound),
    ):
        getImage("pubgrade")


@patch(
    "kubernetes.client.AppsV1Api.read_namespaced_deployment",
    mock_read_namespaced_deployment,
)
@patch("pubgrade_sidecar.controllers.check_image_availability", return_value=True)
@patch("pubgrade_sidecar.controllers._executor")
def test_update_deployment(mock_executor: MagicMock, mock_check: MagicMock) -> None:
    """Test that a notification is accepted and its delivery started."""
    app = Flask(__name__)
    with app.test_request_context(
        "url",
        headers={"X-Access-Token": "test-token"},
        json={"image_name": "akash7778/pubgrade", "tag": "1.0"},
    ):
        response, code = updateDeployment("pubgrade")
        assert code == 202
        assert (
            cast(dict[str, Any], response.json)["message"]
            == "Updating image in progress..."
        )
    # The delivery of the new tag is started, with the patch of the image
    image = "akash7778/pubgrade:1.0"
    mock_executor.submit.assert_called_once()
    function, delivery, *args = mock_executor.submit.call_args.args
    assert function is controllers._deliver
    assert delivery.image == image
    assert args == ["akash7778/pubgrade", "1.0", "pubgrade"]
    # The image is checked in the background only
    mock_check.assert_not_called()
