"""Tests for the Helm chart, on its rendered templates."""

from pathlib import Path
import shutil
import subprocess
from typing import Any

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


def run_helm(settings: list[str]) -> "subprocess.CompletedProcess[str]":
    """Render the chart.

    Args:
        settings: Values to set.

    Returns:
        The finished process.
    """
    command = ["helm", "template", "sidecar", str(CHART), "--namespace", "tes"]
    for setting in settings:
        command += ["--set", setting]
    # A fixed command, with values from the tests only
    return subprocess.run(  # noqa: S603
        command, capture_output=True, text=True, check=False
    )


def render_all(*settings: str) -> list[dict[str, Any]]:
    """Render the chart with the required values.

    Args:
        *settings: Further values to set.

    Returns:
        Rendered resources.
    """
    result = run_helm(REQUIRED + list(settings))
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def render(*settings: str) -> dict[str, dict[str, Any]]:
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
        ([*REQUIRED, "route.enabled=true"], "hostName"),
        ([*REQUIRED, "ingress.enabled=true"], "hostName"),
        (
            [*REQUIRED, "ingress.enabled=true", "hostName=a.example.org"],
            "ingress.tlsSecretName",
        ),
        (
            [
                *REQUIRED,
                "route.enabled=true",
                "ingress.enabled=true",
                "hostName=a.example.org",
            ],
            "not both",
        ),
        ([*REQUIRED, "appName=Not_Valid"], "appName"),
        ([*REQUIRED, "appName=" + "a" * 64], "appName"),
        ([*REQUIRED[1:], "targetDeployments=tesk-api"], "targetDeployments"),
        ([*REQUIRED[1:], "targetDeployments[0]=123"], "targetDeployments"),
    ],
)
def test_incomplete_values_fail(settings: list[str], message: str) -> None:
    """Test that values that can't work fail at rendering, with a reason."""
    result = run_helm(settings)
    assert result.returncode != 0
    assert message in result.stderr


def test_image_tag_defaults_to_app_version() -> None:
    """Without a tag, the image of the chart's `appVersion` is deployed."""
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
    spec = render()["Deployment"]["spec"]["template"]["spec"]
    image = spec["containers"][0]["image"]
    assert image == f"elixircloud/pubgrade-sidecar:{chart['appVersion']}"


def test_image_tag_can_be_set() -> None:
    """A tag that is set replaces the chart's `appVersion`."""
    spec = render("image.tag=sha-abc")["Deployment"]["spec"]["template"]["spec"]
    assert spec["containers"][0]["image"] == "elixircloud/pubgrade-sidecar:sha-abc"


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
    assert "runAsUser" not in spec["containers"][0]["securityContext"]


def test_security_contexts_default() -> None:
    """Test that the pod and container are restricted by default."""
    spec = render()["Deployment"]["spec"]["template"]["spec"]
    assert spec["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert spec["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }


@pytest.mark.parametrize(
    "setting",
    [
        "podSecurityContext.seccompProfile=null",
        "podSecurityContext.seccompProfile.type=null",
    ],
)
def test_seccomp_profile_can_be_removed(setting: str) -> None:
    """Test that a removed or empty seccomp profile isn't rendered.

    Clusters that set their own profile reject pods that name one (e.g., CSC
    Rahti); the rest of the security context stays.

    Args:
        setting: Value that removes the profile or its type.
    """
    spec = render(setting)["Deployment"]["spec"]["template"]["spec"]
    assert spec["securityContext"] == {"runAsNonRoot": True}
    assert spec["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }


def test_security_contexts_can_be_set() -> None:
    """Test that the security contexts take further settings."""
    spec = render(
        "podSecurityContext.fsGroup=1001",
        "securityContext.readOnlyRootFilesystem=true",
    )["Deployment"]["spec"]["template"]["spec"]
    assert spec["securityContext"]["fsGroup"] == 1001
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    container = spec["containers"][0]["securityContext"]
    assert container["readOnlyRootFilesystem"] is True
    assert container["allowPrivilegeEscalation"] is False


def test_security_contexts_can_be_removed() -> None:
    """Test that security contexts set to null aren't rendered."""
    resources = render("podSecurityContext=null", "securityContext=null")
    spec = resources["Deployment"]["spec"]["template"]["spec"]
    assert "securityContext" not in spec
    assert "securityContext" not in spec["containers"][0]


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


def test_targets_and_label_reach_sidecar() -> None:
    """Test that the sidecar knows its deployments and their label."""
    resources = render("targetDeployments[1]=tesk-worker", "deploymentLabel=tier=api")
    container = resources["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    variables = {variable["name"]: variable for variable in container["env"]}
    assert variables["TARGET_DEPLOYMENTS"]["value"] == "tesk-api,tesk-worker"
    assert variables["DEPLOYMENT_LABEL"]["value"] == "tier=api"


def test_no_label_by_default() -> None:
    """Test that without a label, all target deployments count."""
    container = render()["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert "DEPLOYMENT_LABEL" not in {variable["name"] for variable in container["env"]}


def test_registry_credentials_mounted_from_secret() -> None:
    """Test that registry credentials come from a mounted Secret."""
    resources = render(
        "registryCredentials.existingSecret=registry-auth",
        "insecureRegistries[0]=registry.local:5000",
        "insecureRegistries[1]=mirror.local",
    )
    pod = resources["Deployment"]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    variables = {variable["name"]: variable for variable in container["env"]}
    assert variables["REGISTRY_AUTH_FILE"]["value"] == (
        "/etc/pubgrade-sidecar/registry/.dockerconfigjson"
    )
    assert variables["INSECURE_REGISTRIES"]["value"] == (
        "registry.local:5000,mirror.local"
    )
    assert container["volumeMounts"] == [
        {
            "name": "registry-credentials",
            "mountPath": "/etc/pubgrade-sidecar/registry",
            "readOnly": True,
        }
    ]
    assert pod["volumes"] == [
        {
            "name": "registry-credentials",
            "secret": {
                "secretName": "registry-auth",
                "items": [{"key": ".dockerconfigjson", "path": ".dockerconfigjson"}],
            },
        }
    ]


def test_registry_credentials_key_can_be_set() -> None:
    """Test that the key of the credentials in the Secret can be set."""
    pod = render(
        "registryCredentials.existingSecret=registry-auth",
        "registryCredentials.key=config.json",
    )["Deployment"]["spec"]["template"]["spec"]
    assert pod["volumes"][0]["secret"]["items"] == [
        {"key": "config.json", "path": ".dockerconfigjson"}
    ]


def test_no_registry_settings_by_default() -> None:
    """Test that by default, registries are reached anonymously over HTTPS."""
    pod = render()["Deployment"]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    names = {variable["name"] for variable in container["env"]}
    assert not names & {"REGISTRY_AUTH_FILE", "INSECURE_REGISTRIES"}
    assert "volumeMounts" not in container
    assert "volumes" not in pod
