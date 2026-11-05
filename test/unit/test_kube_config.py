"""Tests for loading the Kubernetes client configuration."""

from unittest.mock import MagicMock, patch

from kubernetes import config
import pytest

from pubgrade_sidecar.controllers import _load_kube_config


@patch("pubgrade_sidecar.controllers.config.load_kube_config")
@patch("pubgrade_sidecar.controllers.config.load_incluster_config")
def test_load_kube_config_in_cluster(
    mock_incluster: MagicMock,
    mock_kube_config: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a pod uses the configuration of the cluster."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
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


@patch("pubgrade_sidecar.controllers.config.load_kube_config")
def test_load_kube_config_outside_cluster(
    mock_kube_config: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that the local configuration is used outside a cluster."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    _load_kube_config()
    mock_kube_config.assert_called_once_with()


@patch(
    "pubgrade_sidecar.controllers.config.load_kube_config",
    side_effect=config.ConfigException("no configuration"),
)
def test_load_kube_config_outside_cluster_missing(
    mock_kube_config: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that an unusable local configuration only logs a warning."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    _load_kube_config()
    mock_kube_config.assert_called_once_with()
    assert "Kubernetes configuration not loaded" in caplog.text
    assert "no configuration" in caplog.text
