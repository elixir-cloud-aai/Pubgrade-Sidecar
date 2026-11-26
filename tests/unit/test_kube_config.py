"""Tests for loading the Kubernetes client configuration."""

import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock, patch

from kubernetes import client, config
import pytest

from pubgrade_sidecar.controllers import _load_kube_config

ROOT = Path(__file__).resolve().parents[2]
# A kubeconfig whose server can't be reached, so that nothing is contacted
KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
  - name: {name}
    cluster:
      server: https://127.0.0.1:1
users:
  - name: {name}
    user:
      token: not-a-token
contexts:
  - name: {name}
    context:
      cluster: {name}
      user: {name}
current-context: {name}
"""


def _write_kubeconfig(path: Path, name: str) -> Path:
    """Write a kubeconfig with one cluster, user and context.

    Args:
        path: The file to write.
        name: The name of the cluster, user and context.

    Returns:
        The path of the file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(KUBECONFIG.format(name=name))
    return path


@patch("pubgrade_sidecar.controllers.config.load_kube_config")
@patch("pubgrade_sidecar.controllers.config.load_incluster_config")
def test_load_kube_config_in_cluster(
    mock_incluster: MagicMock,
    mock_kube_config: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a pod uses the configuration of the cluster."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.delenv("KUBECONFIG", raising=False)
    _load_kube_config()
    mock_incluster.assert_called_once_with()
    mock_kube_config.assert_not_called()


@patch(
    "pubgrade_sidecar.controllers.config.load_incluster_config",
    side_effect=config.ConfigException("no service account"),
)
def test_load_kube_config_in_cluster_fails(
    mock_incluster: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a pod without a usable configuration fails at startup."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    with pytest.raises(config.ConfigException):
        _load_kube_config()
    mock_incluster.assert_called_once_with()


@pytest.mark.parametrize("value", [None, "", "  ", ":", " : "])
@patch("pubgrade_sidecar.controllers.config.load_kube_config")
def test_load_kube_config_outside_cluster_refuses_default(
    mock_kube_config: MagicMock,
    value: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that the default kubeconfig is refused outside a cluster."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    if value is None:
        monkeypatch.delenv("KUBECONFIG", raising=False)
    else:
        monkeypatch.setenv("KUBECONFIG", value)
    with pytest.raises(config.ConfigException, match="KUBECONFIG"):
        _load_kube_config()
    mock_kube_config.assert_not_called()


@patch("pubgrade_sidecar.controllers.config.load_kube_config")
def test_load_kube_config_outside_cluster_explicit(
    mock_kube_config: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that a kubeconfig given in `KUBECONFIG` is used outside a cluster."""
    first = _write_kubeconfig(tmp_path / "first", "first")
    second = _write_kubeconfig(tmp_path / "second", "second")
    kubeconfig = f"{first}:{second}"
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("KUBECONFIG", kubeconfig)
    _load_kube_config()
    mock_kube_config.assert_called_once_with(config_file=kubeconfig)


@patch("pubgrade_sidecar.controllers.config.load_kube_config")
def test_load_kube_config_outside_cluster_missing_file(
    mock_kube_config: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that a `KUBECONFIG` naming a missing file is refused."""
    present = _write_kubeconfig(tmp_path / "present", "present")
    missing = tmp_path / "mistyped"
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("KUBECONFIG", f"{present}:{missing}")
    with pytest.raises(config.ConfigException, match="mistyped"):
        _load_kube_config()
    mock_kube_config.assert_not_called()


def test_load_kube_config_outside_cluster_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that a `KUBECONFIG` that can't be read fails the start.

    A directory stands for any path that exists but can't be read as a file,
    e.g., where Docker created a directory for a mistyped volume source.

    Args:
        monkeypatch: Fixture for changing the environment.
        tmp_path: Directory given as the kubeconfig.
    """
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("KUBECONFIG", str(tmp_path))
    with pytest.raises(OSError):
        _load_kube_config()


def test_load_kube_config_outside_cluster_explicit_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that the client uses the cluster of the given kubeconfig."""
    monkeypatch.setattr(client.Configuration, "_default", None)
    kubeconfig = _write_kubeconfig(tmp_path / "kubeconfig", "given")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    _load_kube_config()
    assert client.Configuration.get_default_copy().host == "https://127.0.0.1:1"


@patch(
    "pubgrade_sidecar.controllers.config.load_kube_config",
    side_effect=config.ConfigException("no configuration"),
)
def test_load_kube_config_outside_cluster_unusable(
    mock_kube_config: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that an unusable kubeconfig that was given only logs a warning."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("KUBECONFIG", os.devnull)
    _load_kube_config()
    mock_kube_config.assert_called_once_with(config_file=os.devnull)
    assert "Kubernetes configuration not loaded" in caplog.text
    assert "no configuration" in caplog.text


def test_sidecar_refuses_to_start_with_default_kubeconfig(tmp_path: Path) -> None:
    """Test that the app doesn't start on the default kubeconfig.

    A kubeconfig in the default location (`~/.kube/config`) with an active
    context must not be used: importing the controllers, as the app does at
    startup, fails and says how to give a kubeconfig.

    Args:
        tmp_path: Directory used as the home directory.
    """
    _write_kubeconfig(tmp_path / ".kube" / "config", "default")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"KUBECONFIG", "KUBERNETES_SERVICE_HOST"}
    }
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(ROOT / "app")
    result = subprocess.run(
        [sys.executable, "-c", "import pubgrade_sidecar.controllers"],
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode != 0
    assert "ConfigException" in result.stderr
    assert "KUBECONFIG" in result.stderr
