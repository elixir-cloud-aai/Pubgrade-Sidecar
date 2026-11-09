"""Tests for the responses of the API, through the app as FOCA creates it."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from connexion import App
from kubernetes.client.rest import ApiException
from munch import DefaultMunch
import pytest

from pubgrade_sidecar import app as sidecar_app
from pubgrade_sidecar import controllers

READ = "kubernetes.client.AppsV1Api.read_namespaced_deployment"
URL = "/deployment/tesk-api"
TOKEN = {"X-Access-Token": "test-token"}
BODY = {"image_name": "elixircloud/tesk-api", "tag": "1.2.0"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """Create a test client of the app, as Gunicorn creates the app.

    The app is created in another working directory, as it must not depend
    on it.

    Args:
        monkeypatch: Fixture for changing the working directory.
        tmp_path: Empty directory, as the working directory.

    Yields:
        The test client.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(controllers, "_executor", MagicMock())
    controllers._deliveries.clear()
    app = sidecar_app.create_app()
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
    """Test that images of other registries are accepted."""
    image = "quay.io/org/app"
    with patch(READ, return_value=deployment(f"{image}:1.0")):
        response = client.put(
            URL, headers=TOKEN, json={"image_name": image, "tag": "2.0"}
        )
    assert response.status_code == 202
    assert response.json["image"] == f"{image}:2.0"


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


@pytest.mark.parametrize(
    "request_args,code",
    [
        ({"headers": {"X-Access-Token": "wrong"}}, 401),
        ({"headers": {}}, 400),
    ],
)
def test_errors_are_problem_details(
    client: Any, request_args: dict[str, Any], code: int
) -> None:
    """Test that errors are problem details (RFC 9457), with title and status."""
    response = client.get(URL, **request_args)
    assert response.status_code == code
    assert response.mimetype == "application/problem+json"
    assert response.json["status"] == code
    assert response.json["title"]


def test_request_body_size_limited(client: Any) -> None:
    """Test that request bodies larger than the configured limit are refused."""
    body = {"image_name": "x" * 70000, "tag": "1.2.0"}
    response = client.put(URL, headers=TOKEN, json=body)
    assert response.status_code == 413
    assert response.mimetype == "application/problem+json"
    assert response.json["status"] == 413
    assert response.json["detail"] == (
        "The request body is larger than the configured limit."
    )
    cast(MagicMock, controllers._executor).submit.assert_not_called()


def test_create_app() -> None:
    """Test that the factory creates the Connexion app with the sidecar's API."""
    app = sidecar_app.create_app()
    assert isinstance(app, App)
    rules = {rule.rule for rule in app.app.url_map.iter_rules()}
    assert "/deployment/<deployment_name>" in rules


def test_main_runs_development_server() -> None:
    """Test that `main()` runs the development server on the configured port."""
    with patch.object(App, "run") as run:
        sidecar_app.main()
    run.assert_called_once_with(port=8080)


def test_notification_for_all_deployments(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that a notification for all deployments is routed and validated."""
    monkeypatch.setenv("TARGET_DEPLOYMENTS", "tesk-api")
    with patch(READ, return_value=deployment("elixircloud/tesk-api:1.1.0")) as read:
        read.return_value.metadata.name = "tesk-api"
        response = client.put("/deployment", headers=TOKEN, json=BODY)
        assert response.status_code == 202
        assert response.json["deployments"] == [
            {"name": "tesk-api", "image": "elixircloud/tesk-api:1.2.0"}
        ]
        assert client.put("/deployment", headers=TOKEN, json={}).status_code == 400
        other = {**BODY, "image_name": "elixircloud/other"}
        assert client.put("/deployment", headers=TOKEN, json=other).status_code == 404


@pytest.mark.parametrize(
    "image_name",
    [
        "elixircloud/tesk-api:1.2.0",
        "elixircloud/tesk-api@sha256:" + "0" * 64,
        "registry:5000/org/app:1.2.0",
    ],
)
@pytest.mark.parametrize("url", [URL, "/deployment"])
def test_notification_with_tag_in_name_rejected(
    client: Any, image_name: str, url: str
) -> None:
    """Test that an image name with a tag or digest gets a 400."""
    body = {"image_name": image_name, "tag": "1.2.0"}
    with patch(READ) as read:
        assert client.put(url, headers=TOKEN, json=body).status_code == 400
    read.assert_not_called()
    cast(MagicMock, controllers._executor).submit.assert_not_called()


def test_notification_with_registry_port_accepted(client: Any) -> None:
    """Test that the port of a registry isn't taken for a tag."""
    image = "registry:5000/org/app"
    with patch(READ, return_value=deployment(f"{image}:1.0")):
        response = client.put(
            URL, headers=TOKEN, json={"image_name": image, "tag": "2.0"}
        )
    assert response.status_code == 202
    assert response.json["image"] == f"{image}:2.0"


@pytest.mark.parametrize("method,body", [("get", None), ("put", BODY)])
def test_other_deployments_refused(
    client: Any, monkeypatch: pytest.MonkeyPatch, method: str, body: Any
) -> None:
    """Test that only the deployments in `TARGET_DEPLOYMENTS` can be used."""
    monkeypatch.setenv("TARGET_DEPLOYMENTS", "tesk-api, tesk-worker")
    call = getattr(client, method)
    with patch(READ, return_value=deployment("elixircloud/tesk-api:1.1.0")) as read:
        response = call("/deployment/other", headers=TOKEN, json=body)
        assert response.status_code == 403
        read.assert_not_called()
        response = call("/deployment/tesk-worker", headers=TOKEN, json=body)
        assert response.status_code in (200, 202)
        read.assert_called_once()


def test_list_limited_to_targets(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that `GET /deployment` lists the deployments in `TARGET_DEPLOYMENTS`.

    They are read one by one (the chart's Role can't list deployments), and
    those that don't exist are left out.
    """
    monkeypatch.setenv("TARGET_DEPLOYMENTS", "tesk-api,missing")

    def read(name: str, namespace: str, **kwargs: Any) -> DefaultMunch:
        """Read a deployment; `missing` doesn't exist.

        Args:
            name: Name of the deployment.
            namespace: Namespace of the deployment.
            **kwargs: Further arguments of the call.

        Returns:
            The deployment.

        Raises:
            ApiException: For `missing`.
        """
        if name == "missing":
            raise ApiException(status=404)
        found = deployment("elixircloud/tesk-api:1.1.0")
        found.metadata.name = name
        return found

    with (
        patch(READ, side_effect=read),
        patch("kubernetes.client.AppsV1Api.list_namespaced_deployment") as listed,
    ):
        response = client.get("/deployment", headers=TOKEN)
    assert response.status_code == 200
    assert response.json == ["tesk-api"]
    listed.assert_not_called()
