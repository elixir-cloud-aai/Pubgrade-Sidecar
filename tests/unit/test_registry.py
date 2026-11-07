"""Tests for the availability check of images in OCI registries."""

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import uuid

import pytest
import requests

from pubgrade_sidecar import controllers
from pubgrade_sidecar.controllers import check_image_availability

HEAD = "pubgrade_sidecar.controllers.requests.head"
GET = "pubgrade_sidecar.controllers.requests.get"
REALM = "https://auth.example.org/token"


def response(
    status: int, headers: dict[str, str] | None = None, body: Any = None
) -> Any:
    """Build a response of the `requests` library.

    Args:
        status: Status code.
        headers: Headers.
        body: JSON body.

    Returns:
        Object with the attributes used by the check.
    """
    mock = MagicMock()
    mock.status_code = status
    mock.headers = headers or {}
    mock.json.return_value = body
    return mock


def bearer(realm: str = REALM) -> Any:
    """Build a response that asks for a bearer token.

    Args:
        realm: URL of the token service.

    Returns:
        The response.
    """
    return response(
        401,
        {
            "WWW-Authenticate": (
                f'Bearer realm="{realm}",service="registry",'
                'scope="repository:org/app:pull"'
            )
        },
    )


@pytest.fixture(autouse=True)
def no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without credentials or insecure registries.

    Args:
        monkeypatch: Fixture for changing the environment.
    """
    monkeypatch.delenv("REGISTRY_AUTH_FILE", raising=False)
    monkeypatch.delenv("INSECURE_REGISTRIES", raising=False)


def auth_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: Any) -> None:
    """Configure a Docker configuration file with credentials.

    Args:
        tmp_path: Directory for the file.
        monkeypatch: Fixture for changing the environment.
        content: Content of the file, as JSON, or a string as is.
    """
    path = tmp_path / "config.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    monkeypatch.setenv("REGISTRY_AUTH_FILE", str(path))


@pytest.mark.parametrize(
    "repository,url",
    [
        (
            "library/nginx",
            "https://registry-1.docker.io/v2/library/nginx/manifests/1",
        ),
        (
            "harbor.example.org/project/app",
            "https://harbor.example.org/v2/project/app/manifests/1",
        ),
        (
            "registry.example.org:5000/a/b/c",
            "https://registry.example.org:5000/v2/a/b/c/manifests/1",
        ),
    ],
)
@patch(HEAD, return_value=response(200))
def test_manifest_of_registry(mock_head: MagicMock, repository: str, url: str) -> None:
    """Test that the manifest is asked for at the image's registry."""
    assert check_image_availability(repository, "1")
    assert mock_head.call_args.args == (url,)
    assert "manifest" in mock_head.call_args.kwargs["headers"]["Accept"]
    assert mock_head.call_args.kwargs["timeout"] > 0


@patch(HEAD, return_value=response(200))
def test_insecure_registry(
    mock_head: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that listed registries are asked over HTTP."""
    monkeypatch.setenv("INSECURE_REGISTRIES", "other:5000, localhost:5000")
    assert check_image_availability("localhost:5000/org/app", "1")
    assert mock_head.call_args.args == ("http://localhost:5000/v2/org/app/manifests/1",)


@pytest.mark.parametrize("status", [404, 500])
@patch(HEAD)
def test_missing_image(mock_head: MagicMock, status: int) -> None:
    """Test that other answers mean that the image isn't available."""
    mock_head.return_value = response(status)
    assert not check_image_availability("org/app", "1")


@patch(HEAD, side_effect=requests.ConnectionError)
def test_network_error(mock_head: MagicMock) -> None:
    """Test that a network error counts as image not available yet."""
    assert not check_image_availability("org/app", "1")


@patch(GET, return_value=response(200, body={"token": "abc"}))
@patch(HEAD)
def test_anonymous_token(mock_head: MagicMock, mock_get: MagicMock) -> None:
    """Test that a token is requested anonymously without credentials."""
    mock_head.side_effect = [bearer(), response(200)]
    assert check_image_availability("org/app", "1")
    assert mock_get.call_args.args == (REALM,)
    assert mock_get.call_args.kwargs["params"] == {
        "service": "registry",
        "scope": "repository:org/app:pull",
    }
    assert mock_get.call_args.kwargs["auth"] is None
    assert mock_head.call_args.kwargs["headers"]["Authorization"] == "Bearer abc"


@pytest.mark.parametrize(
    "entry",
    [
        {"auth": base64.b64encode(b"user:pass:word").decode()},
        {"username": "user", "password": "pass:word"},
    ],
)
@pytest.mark.parametrize("key", ["https://index.docker.io/v1/", "docker.io"])
@patch(GET, return_value=response(200, body={"access_token": "abc"}))
@patch(HEAD)
def test_token_with_credentials(
    mock_head: MagicMock,
    mock_get: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: dict[str, str],
    key: str,
) -> None:
    """Test that a token is requested with the registry's credentials."""
    auth_file(tmp_path, monkeypatch, {"auths": {key: entry}})
    mock_head.side_effect = [bearer(), response(200)]
    assert check_image_availability("org/app", "1")
    assert mock_get.call_args.kwargs["auth"] == ("user", "pass:word")


@patch(GET, return_value=response(200, body={"token": "abc"}))
@patch(HEAD)
def test_credentials_of_other_registry_unused(
    mock_head: MagicMock,
    mock_get: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that credentials go only to their own registry."""
    auth_file(
        tmp_path,
        monkeypatch,
        {"auths": {"other.example.org": {"username": "user", "password": "pw"}}},
    )
    mock_head.side_effect = [bearer(), response(200)]
    assert check_image_availability("harbor.example.org/org/app", "1")
    assert mock_get.call_args.kwargs["auth"] is None


@patch(GET, return_value=response(200, body={"token": "abc"}))
@patch(HEAD)
def test_credentials_not_sent_in_plain_text(
    mock_head: MagicMock,
    mock_get: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that credentials aren't sent to a token service over HTTP."""
    auth_file(
        tmp_path,
        monkeypatch,
        {"auths": {"harbor.example.org": {"username": "user", "password": "pw"}}},
    )
    mock_head.side_effect = [bearer("http://auth.example.org/token"), response(200)]
    assert check_image_availability("harbor.example.org/org/app", "1")
    assert mock_get.call_args.kwargs["auth"] is None


@patch(GET, return_value=response(200, body={"token": "abc"}))
@patch(HEAD)
def test_credentials_to_insecure_registry_itself(
    mock_head: MagicMock,
    mock_get: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that an insecure registry's own token service gets credentials."""
    monkeypatch.setenv("INSECURE_REGISTRIES", "localhost:5000")
    auth_file(
        tmp_path,
        monkeypatch,
        {"auths": {"localhost:5000": {"username": "user", "password": "pw"}}},
    )
    mock_head.side_effect = [bearer("http://localhost:5000/token"), response(200)]
    assert check_image_availability("localhost:5000/org/app", "1")
    assert mock_get.call_args.kwargs["auth"] == ("user", "pw")


@pytest.mark.parametrize(
    "token_response",
    [response(403), response(200, body={}), response(200, body=["no", "object"])],
)
@patch(HEAD)
def test_token_refused(mock_head: MagicMock, token_response: Any) -> None:
    """Test that the image isn't available without a token."""
    mock_head.side_effect = [bearer(), response(200)]
    with patch(GET, return_value=token_response):
        assert not check_image_availability("org/app", "1")


@patch(HEAD)
def test_basic_authentication(
    mock_head: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that registries with basic authentication get the credentials."""
    auth_file(
        tmp_path,
        monkeypatch,
        {"auths": {"https://harbor.example.org": {"username": "u", "password": "p"}}},
    )
    mock_head.side_effect = [
        response(401, {"WWW-Authenticate": 'Basic realm="registry"'}),
        response(200),
    ]
    assert check_image_availability("harbor.example.org/org/app", "1")
    assert mock_head.call_args.kwargs["auth"] == ("u", "p")


@patch(HEAD)
def test_basic_authentication_without_credentials(mock_head: MagicMock) -> None:
    """Test that basic authentication without credentials fails."""
    mock_head.return_value = response(401, {"WWW-Authenticate": "Basic"})
    assert not check_image_availability("harbor.example.org/org/app", "1")
    assert mock_head.call_count == 1


@pytest.mark.parametrize(
    "content",
    ["not json", {"auths": {"docker.io": {"auth": "!!"}}}, {"auths": []}, "[]"],
)
@patch(GET, return_value=response(200, body={"token": "abc"}))
@patch(HEAD)
def test_unreadable_credentials(
    mock_head: MagicMock,
    mock_get: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    content: Any,
) -> None:
    """Test that unreadable credentials are logged, without their content."""
    auth_file(tmp_path, monkeypatch, content)
    mock_head.side_effect = [bearer(), response(200)]
    assert check_image_availability("org/app", "1")
    assert mock_get.call_args.kwargs["auth"] is None
    assert "Registry credentials could not be read." in caplog.messages
    assert "!!" not in caplog.text


@patch(HEAD)
def test_missing_credentials_file(
    mock_head: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that a missing file means no credentials."""
    monkeypatch.setenv("REGISTRY_AUTH_FILE", str(tmp_path / "missing.json"))
    mock_head.return_value = response(200)
    assert check_image_availability("org/app", "1")


def push(registry: str, repository: str, tag: str) -> None:
    """Push a minimal image to a registry, with its HTTP API.

    Args:
        registry: Host and port of the registry, served over HTTP.
        repository: Name of the repository in the registry.
        tag: Tag of the image.
    """
    base = f"http://{registry}/v2/{repository}"
    config = b"{}"
    digest = "sha256:" + hashlib.sha256(config).hexdigest()
    upload = requests.post(f"{base}/blobs/uploads/", timeout=10)
    upload.raise_for_status()
    location = upload.headers["Location"]
    if location.startswith("/"):
        location = f"http://{registry}{location}"
    separator = "&" if "?" in location else "?"
    requests.put(
        f"{location}{separator}digest={digest}",
        data=config,
        headers={"Content-Type": "application/octet-stream"},
        timeout=10,
    ).raise_for_status()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": digest,
            "size": len(config),
        },
        "layers": [],
    }
    requests.put(
        f"{base}/manifests/{tag}",
        data=json.dumps(manifest),
        headers={"Content-Type": "application/vnd.oci.image.manifest.v1+json"},
        timeout=10,
    ).raise_for_status()


@pytest.mark.skipif(
    not os.getenv("OCI_REGISTRY"),
    reason="No OCI registry (`OCI_REGISTRY`, e.g., `localhost:5000`)",
)
def test_generic_oci_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test the check against a real registry, e.g., `registry:2` in CI."""
    registry = os.environ["OCI_REGISTRY"]
    monkeypatch.setenv("INSECURE_REGISTRIES", registry)
    repository = f"pubgrade-sidecar/test-{uuid.uuid4().hex[:8]}"
    push(registry, repository, "1.0")
    assert controllers.check_image_availability(f"{registry}/{repository}", "1.0")
    assert not controllers.check_image_availability(f"{registry}/{repository}", "2.0")
