"""Tests for the responses of the API, through the app as FOCA creates it."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from foca import Foca
from kubernetes.client.rest import ApiException
from munch import DefaultMunch
import pytest

from pubgrade_sidecar import controllers

APP_DIR = Path(__file__).resolve().parents[2] / "app"
READ = "kubernetes.client.AppsV1Api.read_namespaced_deployment"
URL = "/deployment/tesk-api"
TOKEN = {"X-Access-Token": "test-token"}
BODY = {"image_name": "elixircloud/tesk-api", "tag": "1.2.0"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Create a test client of the app.

    Args:
        monkeypatch: Fixture for changing the working directory.

    Yields:
        The test client.
    """
    monkeypatch.chdir(APP_DIR)
    monkeypatch.setattr(controllers, "_executor", MagicMock())
    controllers._deliveries.clear()
    app = Foca(config_file="config/config.yaml").create_app()
    yield app.app.test_client()
    controllers._deliveries.clear()


def deployment(image: str) -> DefaultMunch:
    """Build a healthy Deployment as returned by the Kubernetes client.

    Args:
        image: Image of the first container.

    Returns:
        Object with the attributes used by the controllers.
    """
    return DefaultMunch.fromDict(
        {
            "metadata": {"generation": 1},
            "spec": {
                "replicas": 1,
                "template": {"spec": {"containers": [{"image": image}]}},
            },
            "status": {
                "observed_generation": 1,
                "updated_replicas": 1,
                "replicas": 1,
                "available_replicas": 1,
            },
        }
    )


@pytest.mark.parametrize(
    "request_args,code",
    [
        ({"method": "GET", "headers": {}}, 400),
        ({"method": "GET", "headers": {"X-Access-Token": "wrong"}}, 401),
        ({"method": "PUT", "headers": {"X-Access-Token": "wrong"}, "json": BODY}, 401),
        ({"method": "PUT", "headers": TOKEN, "json": {}}, 400),
        (
            {
                "method": "PUT",
                "headers": TOKEN,
                "data": "x",
                "content_type": "text/plain",
            },
            415,
        ),
        ({"method": "POST", "headers": TOKEN}, 405),
    ],
)
def test_client_errors(client: Any, request_args: dict[str, Any], code: int) -> None:
    """Test that wrong requests get the status code for client errors."""
    with patch(READ, return_value=deployment("elixircloud/tesk-api:1.1.0")):
        assert client.open(URL, **request_args).status_code == code


@pytest.mark.parametrize("status,code", [(404, 404), (403, 403), (503, 500)])
def test_kubernetes_errors(client: Any, status: int, code: int) -> None:
    """Test that only a missing deployment or right is a client error."""
    with patch(READ, side_effect=ApiException(status=status)):
        assert client.get(URL, headers=TOKEN).status_code == code
        assert client.put(URL, headers=TOKEN, json=BODY).status_code == code


@pytest.mark.parametrize("status,code", [(404, 404), (403, 403), (503, 500)])
def test_kubernetes_errors_of_list_and_delete(
    client: Any, status: int, code: int
) -> None:
    """Test that the other calls treat Kubernetes errors the same way."""
    error = ApiException(status=status)
    with patch(
        "kubernetes.client.AppsV1Api.list_namespaced_deployment", side_effect=error
    ):
        assert client.get("/deployment", headers=TOKEN).status_code == code
    with patch("kubernetes.client.CoreV1Api.delete_namespaced_pod", side_effect=error):
        assert client.delete(URL, headers=TOKEN).status_code == code


def test_notification_accepted(client: Any) -> None:
    """Test that a notification for the deployment's repository is accepted."""
    with patch(READ, return_value=deployment("elixircloud/tesk-api:1.1.0")):
        response = client.put(URL, headers=TOKEN, json=BODY)
    assert response.status_code == 202
    assert response.json["image"] == "elixircloud/tesk-api:1.2.0"


@pytest.mark.parametrize(
    "image", ["other/image:1.0", "quay.io/elixircloud/tesk-api:1.1.0"]
)
def test_notification_rejected(client: Any, image: str) -> None:
    """Test that a notification that can't apply changes nothing."""
    with patch(READ, return_value=deployment(image)):
        response = client.put(URL, headers=TOKEN, json=BODY)
    assert response.status_code == 409
    cast(MagicMock, controllers._executor).submit.assert_not_called()


def test_notification_for_other_registry(client: Any) -> None:
    """Test that images on other registries are rejected up front."""
    image = "quay.io/org/app"
    with patch(READ, return_value=deployment(f"{image}:1.0")):
        response = client.put(
            URL, headers=TOKEN, json={"image_name": image, "tag": "2.0"}
        )
    assert response.status_code == 409
    assert "Docker Hub" in response.json["msg"]
    cast(MagicMock, controllers._executor).submit.assert_not_called()


def test_status_report(client: Any) -> None:
    """Test the status report of a healthy deployment."""
    with patch(READ, return_value=deployment("elixircloud/tesk-api:1.1.0")):
        response = client.get(URL, headers=TOKEN)
    assert response.status_code == 200
    assert response.json == {
        "image": "elixircloud/tesk-api:1.1.0",
        "status": "complete",
        "replicas": 1,
        "updated_replicas": 1,
        "available_replicas": 1,
    }
