"""Tests for the rollout policy: refused downgrades and allowed tags."""

from collections.abc import Iterator
import logging
from typing import Any, cast
from unittest.mock import MagicMock, patch

from flask import Flask
from foca.errors import InternalServerError
from munch import DefaultMunch
import pytest

from pubgrade_sidecar import controllers
from pubgrade_sidecar.errors.exceptions import (
    DOWNGRADE_DETAIL,
    DowngradeRefused,
    TagNotAllowed,
)

TOKEN = "test-token"
READ = "kubernetes.client.AppsV1Api.read_namespaced_deployment"
PATCH = "kubernetes.client.AppsV1Api.patch_namespaced_deployment"
CHECK = "pubgrade_sidecar.controllers.check_image"
# Outcomes of the availability check
AVAILABLE = controllers.ImageCheck(available=True)
MISSING = controllers.ImageCheck(available=False, reason="registry answered 404")
REPOSITORY = "elixircloud/tesk-api"
DIGEST = "sha256:" + "a" * 64


class ImmediateExecutor:
    """Executor that runs submitted functions at once, in the caller."""

    def submit(self, function: Any, *args: Any) -> None:
        """Run a function.

        Args:
            function: Function to run.
            *args: Positional arguments of the function.
        """
        function(*args)


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


def deployment(name: str, images: list[str]) -> DefaultMunch:
    """Build a Deployment as returned by the Kubernetes client.

    Args:
        name: Name of the Deployment.
        images: Images of its containers, in order.

    Returns:
        Object with the attributes used by the controllers.
    """
    return DefaultMunch.fromDict(
        {
            "metadata": {"name": name, "generation": 2, "uid": f"uid-{name}"},
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


@pytest.fixture(autouse=True)
def clean_deliveries(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run deliveries at once, without records or settings of other tests.

    Args:
        monkeypatch: Fixture for changing the environment.

    Yields:
        Nothing.
    """
    monkeypatch.setattr(controllers, "_executor", ImmediateExecutor())
    monkeypatch.setenv("TARGET_DEPLOYMENTS", "tesk-api,tesk-worker")
    for name in ("DEPLOYMENT_LABEL", "REFUSE_DOWNGRADES", "ALLOWED_TAGS"):
        monkeypatch.delenv(name, raising=False)
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


@pytest.fixture
def refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn on the refusal of downgrades.

    Args:
        monkeypatch: Fixture for changing the environment.
    """
    monkeypatch.setenv("REFUSE_DOWNGRADES", "true")


def notify_one(app: Flask, tag: str, name: str = "tesk-api") -> Any:
    """Send a notification for one deployment.

    Args:
        app: Flask app.
        tag: Tag of the notification.
        name: Name of the deployment.

    Returns:
        Return value of the controller.
    """
    with app.test_request_context(
        "url",
        headers={"X-Access-Token": TOKEN},
        json={"image_name": REPOSITORY, "tag": tag},
    ):
        return controllers.updateDeployment(name)


def notify_all(app: Flask, tag: str) -> Any:
    """Send a notification for all deployments.

    Args:
        app: Flask app.
        tag: Tag of the notification.

    Returns:
        Return value of the controller.
    """
    with app.test_request_context(
        "url",
        headers={"X-Access-Token": TOKEN},
        json={"image_name": REPOSITORY, "tag": tag},
    ):
        return controllers.updateDeployments()


def reader(deployments: dict[str, DefaultMunch]) -> Any:
    """Build a replacement of the Kubernetes client's read call.

    Args:
        deployments: Deployments by name.

    Returns:
        Function that returns the deployment of a name.
    """

    def read(_: Any, name: str, namespace: str, **kwargs: Any) -> DefaultMunch:
        """Read a deployment.

        Args:
            name: Name of the deployment.
            namespace: Namespace of the deployment.
            **kwargs: Further arguments of the call.

        Returns:
            The deployment.
        """
        return deployments[name]

    return read


@pytest.mark.parametrize(
    "current,tag",
    [
        # Higher, also as a pre-release, in PEP 440 and SemVer spelling
        ("1.1.0", "1.2.0"),
        ("1.1.0", "1.1.1"),
        ("1.1.0", "2.0.0"),
        ("1.1.0", "1.2.0rc1"),
        ("1.1.0", "1.2.0-rc.1"),
        ("1.2.0rc1", "1.2.0"),
        ("1.2.0rc1", "1.2.0rc2"),
        ("1.2.0-alpha", "1.2.0-beta"),
        ("v1.1.0", "v1.2.0"),
        ("1.9.0", "1.10.0"),
        # Post-releases spelled unambiguously
        ("1.1.0", "1.1.0.post1"),
        # Floating tags of the same kind
        ("1.1", "1.2"),
        ("1", "2"),
        # A digest next to the tag doesn't count
        (f"1.1.0@{DIGEST}", "1.2.0"),
    ],
)
@patch(PATCH, return_value=deployment("tesk-api", []))
@patch(CHECK, return_value=AVAILABLE)
def test_upgrade_rolls_out(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    refuse: None,
    current: str,
    tag: str,
) -> None:
    """Test that a higher version rolls out."""
    deployments = {"tesk-api": deployment("tesk-api", [f"{REPOSITORY}:{current}"])}
    with patch(READ, new=reader(deployments)):
        _, code = notify_one(app, tag)
    assert code == 202
    mock_patch.assert_called_once()


@pytest.mark.parametrize(
    "current,tag",
    [
        # Lower
        ("1.2.0", "1.1.0"),
        ("1.2.0", "1.1.9"),
        ("1.10.0", "1.9.0"),
        ("2.0.0", "1.99.99"),
        ("v1.2.0", "1.1.0"),
        # Lower as pre-releases of the same or a lower version
        ("1.2.0", "1.2.0rc1"),
        ("1.2.0", "1.2.0-rc.1"),
        ("1.2.0rc2", "1.2.0rc1"),
        ("1.2.0", "1.2.0.dev1"),
        # Post-releases with a hyphen, which SemVer reads as pre-releases
        ("1.2.0", "1.2.0-1"),
        ("1.2.0", "1.2.0-r1"),
        ("1.2.0", "1.2.0-post1"),
        # Post-releases spelled otherwise than `.postN`
        ("1.2.0", "1.2.0_r1"),
        ("1.2.0", "1.2.0.r1"),
        ("1.2.0", "1.2.0post1"),
        ("1.2.0", "1.2.0_post1"),
        # The same version, spelled otherwise; from a floating tag (`1.2`,
        # maybe 1.2.9 by now) to a release that may be older
        ("1.2.0", "v1.2.0"),
        ("1.2.0", "01.2.0"),
        ("1.2", "1.2.0"),
        ("1", "1.0.0"),
        ("1.2.0", "1.2.0.0"),
        # Other numbers of release segments: build numbers, dates, short
        # commit hashes that read as versions
        ("2.5.0", "20230101"),
        ("2.5.0", "2023.01"),
        ("2.5.0", "12345a6"),
        ("2.5.0", "1234567"),
        ("1.2", "1.3.0"),
        # Tags that aren't versions
        ("1.2.0", "latest"),
        ("1.2.0", "dev"),
        ("1.2.0", "sha-0123abc"),
        ("1.2.0", "1.3.0-alpine"),
        # Current tags that aren't versions, or none
        ("latest", "1.3.0"),
        ("dev", "1.3.0"),
        ("1.2.0-1", "1.3.0"),
        (None, "1.3.0"),
        (f"@{DIGEST}", "1.3.0"),
    ],
)
@patch(PATCH)
@patch(CHECK, return_value=AVAILABLE)
def test_downgrade_refused(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    refuse: None,
    current: str | None,
    tag: str,
) -> None:
    """Test that a lower version, or a tag that isn't one, changes nothing."""
    if current is None:
        image = REPOSITORY
    elif current.startswith("@"):
        image = f"{REPOSITORY}{current}"
    else:
        image = f"{REPOSITORY}:{current}"
    deployments = {"tesk-api": deployment("tesk-api", [image])}
    with patch(READ, new=reader(deployments)), pytest.raises(DowngradeRefused):
        notify_one(app, tag)
    mock_patch.assert_not_called()
    mock_check.assert_not_called()
    assert not controllers._deliveries


@patch(PATCH)
def test_same_tag_is_current(mock_patch: MagicMock, app: Flask, refuse: None) -> None:
    """Test that the tag in use is no rollout, and no refusal."""
    deployments = {"tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.2.0"])}
    with patch(READ, new=reader(deployments)):
        _, code = notify_one(app, "1.2.0")
    assert code == 200
    mock_patch.assert_not_called()


@patch(PATCH, return_value=deployment("tesk-api", []))
@patch(CHECK, return_value=AVAILABLE)
def test_downgrade_allowed_by_default(
    mock_check: MagicMock, mock_patch: MagicMock, app: Flask
) -> None:
    """Test that without the setting, any tag rolls out, as before."""
    deployments = {"tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.2.0"])}
    with patch(READ, new=reader(deployments)):
        assert notify_one(app, "1.0.0")[1] == 202
        assert notify_one(app, "latest")[1] == 202
    assert mock_patch.call_count == 2


@pytest.mark.parametrize("value", ["true", "TRUE", " True ", "1", "yes", "on"])
def test_setting_on(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Test the spellings that turn the setting on."""
    monkeypatch.setenv("REFUSE_DOWNGRADES", value)
    assert controllers._refuse_downgrades()


@pytest.mark.parametrize("value", ["", " ", "false", "FALSE", "0", "no", "off"])
def test_setting_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Test the spellings that leave the setting off."""
    monkeypatch.setenv("REFUSE_DOWNGRADES", value)
    assert not controllers._refuse_downgrades()


def test_unknown_setting_turns_it_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that a misspelt value refuses downgrades, rather than allowing them."""
    monkeypatch.setenv("REFUSE_DOWNGRADES", "ture")
    with caplog.at_level(logging.ERROR):
        assert controllers._refuse_downgrades()
    assert "REFUSE_DOWNGRADES" in caplog.text


@patch(PATCH)
@patch(CHECK, return_value=AVAILABLE)
def test_any_lower_container_refuses(
    mock_check: MagicMock, mock_patch: MagicMock, app: Flask, refuse: None
) -> None:
    """Test that one container with a higher version refuses the whole deployment."""
    deployments = {
        "tesk-api": deployment(
            "tesk-api",
            [f"{REPOSITORY}:1.1.0", "other/proxy:1.0", f"docker.io/{REPOSITORY}:1.3.0"],
        )
    }
    with patch(READ, new=reader(deployments)), pytest.raises(DowngradeRefused):
        notify_one(app, "1.2.0")
    mock_patch.assert_not_called()


@patch(PATCH)
@patch(CHECK, return_value=AVAILABLE)
def test_container_with_the_tag_counts_no_more(
    mock_check: MagicMock, mock_patch: MagicMock, app: Flask, refuse: None
) -> None:
    """Test that a container that has the tag already isn't compared."""
    mock_patch.return_value = deployment("tesk-api", [])
    deployments = {
        "tesk-api": deployment(
            "tesk-api", [f"{REPOSITORY}:1.2.0", f"docker.io/{REPOSITORY}:1.1.0"]
        )
    }
    with patch(READ, new=reader(deployments)):
        assert notify_one(app, "1.2.0")[1] == 202
    replaced = [
        operation["path"]
        for operation in mock_patch.call_args.kwargs["body"]
        if operation["op"] == "replace"
    ]
    assert replaced == ["/spec/template/spec/containers/1/image"]


@patch(PATCH)
@patch(CHECK, return_value=AVAILABLE)
def test_all_deployments_refused_together(
    mock_check: MagicMock, mock_patch: MagicMock, app: Flask, refuse: None
) -> None:
    """Test that one deployment with a higher version refuses the notification."""
    deployments = {
        "tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.1.0"]),
        "tesk-worker": deployment("tesk-worker", [f"{REPOSITORY}:1.3.0"]),
    }
    with patch(READ, new=reader(deployments)), pytest.raises(DowngradeRefused):
        notify_all(app, "1.2.0")
    mock_patch.assert_not_called()
    assert not controllers._deliveries


@patch(PATCH, return_value=deployment("tesk-api", []))
@patch(CHECK, return_value=AVAILABLE)
def test_all_deployments_upgrade(
    mock_check: MagicMock, mock_patch: MagicMock, app: Flask, refuse: None
) -> None:
    """Test that a higher version rolls out to all deployments."""
    deployments = {
        "tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.1.0"]),
        "tesk-worker": deployment("tesk-worker", [f"{REPOSITORY}:1.2.0"]),
    }
    with patch(READ, new=reader(deployments)):
        response, code = notify_all(app, "1.2.0")
    assert code == 202
    assert [item["name"] for item in response.json["deployments"]] == ["tesk-api"]


@patch(PATCH)
@patch(CHECK, return_value=AVAILABLE)
def test_refused_notification_keeps_waiting_delivery(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    refuse: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a refused notification doesn't replace one that still waits."""
    mock_patch.return_value = deployment("tesk-api", [])
    executor = DeferredExecutor()
    monkeypatch.setattr(controllers, "_executor", executor)
    deployments = {"tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.1.0"])}
    with patch(READ, new=reader(deployments)):
        assert notify_one(app, "1.2.0")[1] == 202
        with pytest.raises(DowngradeRefused):
            notify_one(app, "1.0.0")
        executor.run()
    applied = [
        operation["value"]
        for operation in mock_patch.call_args.kwargs["body"]
        if operation["op"] == "replace"
    ]
    assert applied == [f"{REPOSITORY}:1.2.0"]


@pytest.mark.parametrize(
    "tag,code",
    [
        # The current tag would end the waiting delivery, a version between
        # them would replace it
        ("1.1.0", None),
        ("1.1.1", None),
        ("v2.0.0", None),
        # The waiting tag again, or a higher one, may replace it
        ("2.0.0", 202),
        ("2.0.1", 202),
    ],
)
@pytest.mark.parametrize("notify", [notify_one, notify_all])
@patch(PATCH)
@patch(CHECK, return_value=AVAILABLE)
def test_waiting_delivery_not_undercut(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    refuse: None,
    monkeypatch: pytest.MonkeyPatch,
    notify: Any,
    tag: str,
    code: int | None,
) -> None:
    """Test that a notification can't cancel or shrink a waiting upgrade."""
    monkeypatch.setenv("TARGET_DEPLOYMENTS", "tesk-api")
    executor = DeferredExecutor()
    monkeypatch.setattr(controllers, "_executor", executor)
    deployments = {"tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.1.0"])}
    with patch(READ, new=reader(deployments)):
        assert notify_one(app, "2.0.0")[1] == 202
        waiting = controllers._deliveries[("tesk-api", REPOSITORY)]
        if code is None:
            with pytest.raises(DowngradeRefused):
                notify(app, tag)
            assert controllers._deliveries[("tesk-api", REPOSITORY)] is waiting
        else:
            assert notify(app, tag)[1] == code


@patch(PATCH)
@patch(CHECK, return_value=AVAILABLE)
def test_waiting_delivery_refuses_all_deployments(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    refuse: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that undercutting one waiting delivery starts and ends none."""
    executor = DeferredExecutor()
    monkeypatch.setattr(controllers, "_executor", executor)
    deployments = {
        "tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.1.0"]),
        "tesk-worker": deployment("tesk-worker", [f"{REPOSITORY}:1.0.0"]),
    }
    with patch(READ, new=reader(deployments)):
        assert notify_one(app, "2.0.0")[1] == 202
        waiting = controllers._deliveries[("tesk-api", REPOSITORY)]
        # tesk-worker would upgrade, but tesk-api has 2.0.0 waiting
        with pytest.raises(DowngradeRefused):
            notify_all(app, "1.1.0")
    assert controllers._deliveries == {("tesk-api", REPOSITORY): waiting}
    assert len(executor.pending) == 1


@patch(PATCH, return_value=deployment("tesk-api", []))
@patch(CHECK, return_value=AVAILABLE)
def test_finished_delivery_not_compared(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    refuse: None,
) -> None:
    """Test that only a delivery that still waits counts, not a failed one."""
    controllers._deliveries[("tesk-api", REPOSITORY)] = controllers._Delivery(
        image=f"{REPOSITORY}:3.0.0", state="failed", detail="Image not available."
    )
    deployments = {"tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.1.0"])}
    with patch(READ, new=reader(deployments)):
        assert notify_one(app, "1.2.0")[1] == 202


@patch(PATCH, return_value=deployment("tesk-api", []))
@patch(CHECK, return_value=AVAILABLE)
def test_waiting_delivery_ignored_by_default(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that without the setting, a newer notification replaces any other."""
    monkeypatch.setattr(controllers, "_executor", DeferredExecutor())
    deployments = {"tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.1.0"])}
    with patch(READ, new=reader(deployments)):
        assert notify_one(app, "2.0.0")[1] == 202
        assert notify_one(app, "1.1.0")[1] == 200
    assert not controllers._deliveries


@patch(PATCH)
@patch(CHECK, return_value=AVAILABLE)
def test_upgraded_during_the_wait_fails_the_delivery(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    refuse: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that a deployment upgraded meanwhile isn't patched back."""
    executor = DeferredExecutor()
    monkeypatch.setattr(controllers, "_executor", executor)
    deployments = {"tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.1.0"])}
    with patch(READ, new=reader(deployments)):
        assert notify_one(app, "1.2.0")[1] == 202
        deployments["tesk-api"] = deployment("tesk-api", [f"{REPOSITORY}:1.3.0"])
        executor.run()
        with app.test_request_context("url", headers={"X-Access-Token": TOKEN}):
            report = cast(dict[str, Any], controllers.getImage("tesk-api").json)
    mock_patch.assert_not_called()
    assert report["status"] == "failed"
    assert report["requested_image"] == f"{REPOSITORY}:1.2.0"
    assert report["detail"] == DOWNGRADE_DETAIL


@pytest.mark.parametrize(
    "pattern,tag",
    [
        (r"[0-9]+\.[0-9]+\.[0-9]+", "1.2.0"),
        (r"^[0-9]+\.[0-9]+\.[0-9]+$", "1.2.0"),
        (r"v?[0-9.]+(rc[0-9]+)?", "v1.2.0rc1"),
    ],
)
@patch(PATCH, return_value=deployment("tesk-api", []))
@patch(CHECK, return_value=AVAILABLE)
def test_allowed_tag_rolls_out(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    pattern: str,
    tag: str,
) -> None:
    """Test that a tag that matches the pattern rolls out."""
    monkeypatch.setenv("ALLOWED_TAGS", pattern)
    deployments = {"tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.1.0"])}
    with patch(READ, new=reader(deployments)):
        assert notify_one(app, tag)[1] == 202


@pytest.mark.parametrize(
    "pattern,tag",
    [
        # The whole tag must match
        (r"[0-9]+\.[0-9]+\.[0-9]+", "1.2.0rc1"),
        (r"[0-9]+\.[0-9]+\.[0-9]+", "x1.2.0"),
        (r"[0-9]+\.[0-9]+\.[0-9]+", "1.2.0-alpine"),
        (r"1\.2\.0|1\.3\.0", "1.2.0x"),
        (r"[0-9.]+", "latest"),
    ],
)
@pytest.mark.parametrize("notify", [notify_one, notify_all])
@patch(PATCH)
@patch(READ)
def test_other_tag_refused(
    mock_read: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    notify: Any,
    pattern: str,
    tag: str,
) -> None:
    """Test that a tag that doesn't match changes nothing and needs no cluster."""
    monkeypatch.setenv("ALLOWED_TAGS", pattern)
    with pytest.raises(TagNotAllowed):
        notify(app, tag)
    mock_read.assert_not_called()
    mock_patch.assert_not_called()


@patch(READ)
def test_invalid_pattern_refuses_all(
    mock_read: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that a pattern that isn't a regular expression refuses every tag."""
    monkeypatch.setenv("ALLOWED_TAGS", "[0-9")
    with caplog.at_level(logging.ERROR), pytest.raises(InternalServerError):
        notify_one(app, "1.2.0")
    assert "ALLOWED_TAGS" in caplog.text
    mock_read.assert_not_called()


@patch(PATCH, return_value=deployment("tesk-api", []))
@patch(CHECK, return_value=AVAILABLE)
def test_empty_pattern_allows_all(
    mock_check: MagicMock,
    mock_patch: MagicMock,
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that an empty pattern is no limit, as without the setting."""
    monkeypatch.setenv("ALLOWED_TAGS", " ")
    deployments = {"tesk-api": deployment("tesk-api", [f"{REPOSITORY}:1.1.0"])}
    with patch(READ, new=reader(deployments)):
        assert notify_one(app, "anything")[1] == 202
