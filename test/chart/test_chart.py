"""Tests for the Helm chart, on its rendered templates."""

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "deployment"
REQUIRED = [
    "targetDeployments[0]=tesk-api",
    "accessToken.existingSecret=sidecar-token",
]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Helm is not installed"
)


def run_helm(settings: List[str]) -> "subprocess.CompletedProcess[str]":
    """Render the chart.

    Args:
        settings: Values to set.

    Returns:
        The finished process.
    """
    command = ["helm", "template", "sidecar", str(CHART), "--namespace", "tes"]
    for setting in settings:
        command += ["--set", setting]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def render_all(*settings: str) -> List[Dict[str, Any]]:
    """Render the chart with the required values.

    Args:
        *settings: Further values to set.

    Returns:
        Rendered resources.
    """
    result = run_helm(REQUIRED + list(settings))
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def render(*settings: str) -> Dict[str, Dict[str, Any]]:
    """Render the chart with the required values, for one resource per kind.

    Args:
        *settings: Further values to set.

    Returns:
        Rendered resources by kind.
    """
    return {document["kind"]: document for document in render_all(*settings)}


@pytest.mark.parametrize(
    "settings,message",
    [
        ([], "targetDeployments"),
        (REQUIRED[:1], "accessToken.existingSecret"),
        (REQUIRED + ["route.enabled=true"], "hostName"),
        (REQUIRED + ["ingress.enabled=true"], "hostName"),
        (
            REQUIRED + ["ingress.enabled=true", "hostName=a.example.org"],
            "ingress.tlsSecretName",
        ),
        (
            REQUIRED
            + ["route.enabled=true", "ingress.enabled=true", "hostName=a.example.org"],
            "not both",
        ),
        (REQUIRED + ["appName=Not_Valid"], "appName"),
        (REQUIRED + ["appName=" + "a" * 64], "appName"),
        (REQUIRED[1:] + ["targetDeployments=tesk-api"], "targetDeployments"),
        (REQUIRED[1:] + ["targetDeployments[0]=123"], "targetDeployments"),
    ],
)
def test_incomplete_values_fail(settings: List[str], message: str) -> None:
    """Test that values that can't work fail at rendering, with a reason."""
    result = run_helm(settings)
    assert result.returncode != 0
    assert message in result.stderr


def test_role_is_minimal() -> None:
    """Test that the sidecar can only read and patch the target Deployments."""
    role = render("targetDeployments[1]=other")["Role"]
    assert role["rules"] == [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "resourceNames": ["tesk-api", "other"],
            "verbs": ["get", "patch"],
        }
    ]


def test_no_cluster_wide_or_admin_rights() -> None:
    """Test that the chart binds nothing but its own Role."""
    kinds = sorted(document["kind"] for document in render_all())
    assert kinds == ["Deployment", "Role", "RoleBinding", "Service", "ServiceAccount"]
    assert render()["RoleBinding"]["roleRef"]["kind"] == "Role"


def test_no_fixed_user() -> None:
    """Test that the pod works with the user that OpenShift assigns."""
    spec = render()["Deployment"]["spec"]["template"]["spec"]
    assert "runAsUser" not in spec["securityContext"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert "runAsUser" not in spec["containers"][0]["securityContext"]


def test_service_reaches_container() -> None:
    """Test that the Service uses the named port of the container."""
    resources = render("service.port=9000")
    port = resources["Service"]["spec"]["ports"][0]
    assert port["port"] == 9000
    assert port["targetPort"] == "http"
    container = resources["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert container["ports"][0]["name"] == "http"


def test_token_comes_from_secret() -> None:
    """Test that the token is read from the Secret, not from the values."""
    container = render()["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    environment = {variable["name"]: variable for variable in container["env"]}
    assert environment["ACCESS_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "sidecar-token",
        "key": "access-token",
    }
    assert environment["NAMESPACE"]["value"] == "tes"


def test_not_exposed_by_default() -> None:
    """Test that the default is a ClusterIP Service only."""
    resources = render()
    assert resources["Service"]["spec"]["type"] == "ClusterIP"
    assert "Route" not in resources
    assert "Ingress" not in resources


def test_route_terminates_tls() -> None:
    """Test that a Route uses TLS and redirects HTTP."""
    resources = render("route.enabled=true", "hostName=a.example.org")
    spec = resources["Route"]["spec"]
    assert spec["host"] == "a.example.org"
    assert spec["tls"] == {
        "insecureEdgeTerminationPolicy": "Redirect",
        "termination": "edge",
    }
    assert "Ingress" not in resources


def test_ingress_uses_tls() -> None:
    """Test that an Ingress uses TLS for its host."""
    resources = render(
        "ingress.enabled=true",
        "hostName=a.example.org",
        "ingress.tlsSecretName=sidecar-tls",
        "ingress.className=nginx",
    )
    spec = resources["Ingress"]["spec"]
    assert spec["tls"] == [{"hosts": ["a.example.org"], "secretName": "sidecar-tls"}]
    assert spec["ingressClassName"] == "nginx"
    assert "Route" not in resources
