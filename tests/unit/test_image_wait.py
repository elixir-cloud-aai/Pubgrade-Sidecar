"""Tests for the wait for a notified image: its settings, logs and reasons."""

from collections.abc import Iterator
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pubgrade_sidecar import controllers
from pubgrade_sidecar.errors.exceptions import ImageUnavailable

CHECK = "pubgrade_sidecar.controllers.check_image"
SLEEP = "pubgrade_sidecar.controllers.time.sleep"
REPOSITORY = "elixircloud/tesk-api"
KEY = ("tesk-api", REPOSITORY)
AVAILABLE = controllers.ImageCheck(available=True)
RATE_LIMITED = controllers.ImageCheck(
    available=False,
    reason="registry answered 429 (rate limit reached; ratelimit-remaining: 0)",
)


def limited(retry_after: float) -> controllers.ImageCheck:
    """Build the outcome of a check that hit a rate limit with `Retry-After`.

    Args:
        retry_after: Seconds that the registry asks to wait.

    Returns:
        The outcome.
    """
    return controllers.ImageCheck(
        available=False,
        reason=f"registry answered 429 (retry-after: {retry_after})",
        retry_after=retry_after,
    )


@pytest.fixture(autouse=True)
def default_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start every test with the default wait and no recorded deliveries.

    Args:
        monkeypatch: Fixture for changing the environment.

    Yields:
        Nothing.
    """
    monkeypatch.delenv("IMAGE_WAIT_RETRIES", raising=False)
    monkeypatch.delenv("IMAGE_WAIT_INTERVAL", raising=False)
    controllers._deliveries.clear()
    yield
    controllers._deliveries.clear()


def wait(delivery: controllers._Delivery | None = None) -> bool:
    """Wait for the image of a recorded delivery.

    Args:
        delivery: Record of the delivery; a new one by default.

    Returns:
        What `_wait_for_image` returns.
    """
    delivery = delivery or controllers._Delivery(image=f"{REPOSITORY}:1.2.0")
    controllers._deliveries[KEY] = delivery
    return controllers._wait_for_image(REPOSITORY, "1.2.0", delivery, "tesk-api")


def test_defaults_unchanged() -> None:
    """Test that by default, the sidecar checks 4 times, 30 s apart, as before."""
    assert controllers._image_wait() == (3, 30.0)


@pytest.mark.parametrize(
    "retries,interval,expected",
    [
        ("0", "5", (0, 5.0)),
        ("10", "2.5", (10, 2.5)),
        (" 2 ", " 60 ", (2, 60.0)),
    ],
)
def test_settings_read(
    monkeypatch: pytest.MonkeyPatch,
    retries: str,
    interval: str,
    expected: tuple[int, float],
) -> None:
    """Test that the wait is read from the environment."""
    monkeypatch.setenv("IMAGE_WAIT_RETRIES", retries)
    monkeypatch.setenv("IMAGE_WAIT_INTERVAL", interval)
    assert controllers._image_wait() == expected


@pytest.mark.parametrize(
    "name,value",
    [
        ("IMAGE_WAIT_RETRIES", "-1"),
        ("IMAGE_WAIT_RETRIES", "many"),
        ("IMAGE_WAIT_RETRIES", "1.5"),
        ("IMAGE_WAIT_INTERVAL", "0"),
        ("IMAGE_WAIT_INTERVAL", "-3"),
        ("IMAGE_WAIT_INTERVAL", "nan"),
        ("IMAGE_WAIT_INTERVAL", "inf"),
        ("IMAGE_WAIT_INTERVAL", "soon"),
    ],
)
def test_invalid_settings_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    value: str,
) -> None:
    """Test that an invalid setting keeps its default, with a warning."""
    monkeypatch.setenv(name, value)
    with caplog.at_level(logging.WARNING):
        assert controllers._image_wait() == (3, 30.0)
    assert any(name in message for message in caplog.messages)


@patch(SLEEP)
@patch(CHECK, return_value=RATE_LIMITED)
def test_each_failed_check_logged(
    mock_check: MagicMock,
    mock_sleep: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that every failed check is logged once, with its reason."""
    with caplog.at_level(logging.WARNING), pytest.raises(ImageUnavailable) as error:
        wait()
    failed = [message for message in caplog.messages if "429" in message]
    assert len(failed) == 4
    assert failed[0] == (
        "Check 1 of 4 for image elixircloud/tesk-api:1.2.0 failed: registry "
        "answered 429 (rate limit reached; ratelimit-remaining: 0)"
    )
    assert error.value.reason == RATE_LIMITED.reason
    assert [call.args for call in mock_sleep.call_args_list] == [(30.0,)] * 3


@patch(SLEEP)
@patch(CHECK, return_value=RATE_LIMITED)
def test_configured_wait(
    mock_check: MagicMock,
    mock_sleep: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that the configured number of checks and interval apply."""
    monkeypatch.setenv("IMAGE_WAIT_RETRIES", "1")
    monkeypatch.setenv("IMAGE_WAIT_INTERVAL", "5")
    with pytest.raises(ImageUnavailable):
        wait()
    assert mock_check.call_count == 2
    mock_sleep.assert_called_once_with(5.0)


@patch(SLEEP)
@patch(CHECK, return_value=RATE_LIMITED)
def test_no_retries(
    mock_check: MagicMock,
    mock_sleep: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that without retries, the sidecar checks once and doesn't wait."""
    monkeypatch.setenv("IMAGE_WAIT_RETRIES", "0")
    with pytest.raises(ImageUnavailable):
        wait()
    mock_check.assert_called_once()
    mock_sleep.assert_not_called()


@patch(SLEEP)
@patch(CHECK)
def test_available_after_failures(mock_check: MagicMock, mock_sleep: MagicMock) -> None:
    """Test that an image that turns up during the wait counts."""
    mock_check.side_effect = [RATE_LIMITED, RATE_LIMITED, AVAILABLE]
    assert wait()
    assert mock_sleep.call_count == 2


@patch(SLEEP)
@patch(CHECK)
def test_retry_after_honoured(mock_check: MagicMock, mock_sleep: MagicMock) -> None:
    """Test that a longer `Retry-After` is waited for, within the budget."""
    mock_check.side_effect = [limited(50), AVAILABLE]
    assert wait()
    mock_sleep.assert_called_once_with(50)


@patch(SLEEP)
@patch(CHECK)
def test_short_retry_after_keeps_interval(
    mock_check: MagicMock, mock_sleep: MagicMock
) -> None:
    """Test that a shorter `Retry-After` doesn't make the checks more frequent."""
    mock_check.side_effect = [limited(1), AVAILABLE]
    assert wait()
    mock_sleep.assert_called_once_with(30.0)


@patch(SLEEP)
@patch(CHECK)
def test_retry_after_beyond_budget_gives_up(
    mock_check: MagicMock,
    mock_sleep: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that a `Retry-After` beyond the remaining wait ends it at once."""
    mock_check.side_effect = [limited(91)]
    with caplog.at_level(logging.WARNING), pytest.raises(ImageUnavailable) as error:
        wait()
    mock_sleep.assert_not_called()
    assert error.value.reason == (
        "registry answered 429 (retry-after: 91); it asks to wait 91 s, "
        "longer than the remaining wait of 90 s"
    )


@patch(SLEEP)
@patch(CHECK)
def test_total_wait_within_budget(mock_check: MagicMock, mock_sleep: MagicMock) -> None:
    """Test that the waits never add up to more than retries times interval."""
    mock_check.side_effect = [limited(80), RATE_LIMITED]
    with pytest.raises(ImageUnavailable) as error:
        wait()
    # 80 s of the 90 s are used; the next interval of 30 s doesn't fit
    assert [call.args for call in mock_sleep.call_args_list] == [(80,)]
    assert mock_check.call_count == 2
    assert error.value.reason is not None
    assert "remaining wait of 10 s" in error.value.reason


@patch(SLEEP)
@patch(CHECK, return_value=RATE_LIMITED)
def test_replaced_delivery_stops_waiting(
    mock_check: MagicMock, mock_sleep: MagicMock
) -> None:
    """Test that a delivery replaced during the wait stops without failing."""
    delivery = controllers._Delivery(image=f"{REPOSITORY}:1.2.0")

    def replace(*_: Any) -> None:
        controllers._deliveries[KEY] = controllers._Delivery(image="other")

    mock_sleep.side_effect = replace
    assert not wait(delivery)
    mock_check.assert_called_once()


@patch(SLEEP)
@patch(CHECK, return_value=RATE_LIMITED)
@patch(
    "kubernetes.client.AppsV1Api.read_namespaced_deployment",
    side_effect=AssertionError("not read"),
)
def test_reason_in_status(
    mock_read: MagicMock,
    mock_check: MagicMock,
    mock_sleep: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that a failed delivery reports the reason of its last check."""
    delivery = controllers._Delivery(image=f"{REPOSITORY}:1.2.0")
    controllers._deliveries[KEY] = delivery
    with caplog.at_level(logging.WARNING):
        controllers._deliver(delivery, REPOSITORY, "1.2.0", "tesk-api")
    assert delivery.state == "failed"
    assert delivery.detail == (
        "Image not available, or its registry not reachable: registry answered "
        "429 (rate limit reached; ratelimit-remaining: 0)."
    )
    assert (
        "Image elixircloud/tesk-api:1.2.0 isn't available: registry answered 429 "
        "(rate limit reached; ratelimit-remaining: 0)."
    ) in caplog.messages
    mock_read.assert_not_called()


def test_unavailable_without_reason() -> None:
    """Test that a failure without a reason keeps the general explanation."""
    delivery = controllers._Delivery(image=f"{REPOSITORY}:1.2.0")
    controllers._deliveries[KEY] = delivery
    with patch.object(controllers, "_wait_for_image", side_effect=ImageUnavailable()):
        controllers._deliver(delivery, REPOSITORY, "1.2.0", "tesk-api")
    assert delivery.detail == "Image not available, or its registry not reachable."


@patch(SLEEP)
@patch(CHECK, return_value=RATE_LIMITED)
def test_fractional_interval_keeps_all_checks(
    mock_check: MagicMock,
    mock_sleep: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that rounding of fractional intervals doesn't drop a check."""
    monkeypatch.setenv("IMAGE_WAIT_RETRIES", "3")
    monkeypatch.setenv("IMAGE_WAIT_INTERVAL", "0.7")
    with pytest.raises(ImageUnavailable):
        wait()
    assert mock_check.call_count == 4
    assert mock_sleep.call_count == 3


def _answer(status: int, headers: dict[str, str] | None = None) -> MagicMock:
    """Build a registry's answer of the `requests` library.

    Args:
        status: Status code.
        headers: Headers.

    Returns:
        Object with the attributes used by the check.
    """
    answer = MagicMock()
    answer.status_code = status
    answer.headers = headers or {}
    answer.history = []
    answer.iter_content.return_value = [b'{"token": "secret-token"}']
    return answer


CHALLENGE = _answer(
    401,
    {
        "WWW-Authenticate": (
            'Bearer realm="https://auth.example.org/token",service="registry"'
        )
    },
)


@pytest.mark.parametrize(
    "head,get,reason",
    [
        (
            [CHALLENGE, _answer(404)],
            _answer(200),
            "registry answered 404 (image or tag not found)",
        ),
        (
            [CHALLENGE],
            _answer(401),
            "token service answered 401 (authentication failed)",
        ),
        (
            [CHALLENGE, _answer(429, {"RateLimit-Remaining": "0;w=3600"})],
            _answer(200),
            "registry answered 429 (rate limit reached; ratelimit-remaining: 0;w=3600)",
        ),
        (
            controllers.requests.ConnectionError(
                "https://user:password@registry.example.org/v2/"
            ),
            _answer(200),
            "registry or token service not reachable (ConnectionError)",
        ),
        (
            controllers.requests.Timeout("password=hunter2"),
            _answer(200),
            "registry or token service not reachable (Timeout)",
        ),
    ],
)
@patch(SLEEP)
def test_outcome_logged_and_reported_without_secrets(
    mock_sleep: MagicMock,
    head: Any,
    get: MagicMock,
    reason: str,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test each outcome end to end: logs per check, state, and no secrets.

    With credentials configured, the real check runs against mocked
    requests; each check is logged once with its reason, the delivery fails
    with the reason in `detail`, and neither the logs nor the status carry
    the token, the credentials or the URLs of errors.
    """
    auth = tmp_path / "config.json"
    auth.write_text(
        '{"auths": {"docker.io": {"username": "robot", '
        '"password": "registry-password"}}}'
    )
    monkeypatch.setenv("REGISTRY_AUTH_FILE", str(auth))
    monkeypatch.setenv("IMAGE_WAIT_RETRIES", "1")
    monkeypatch.delenv("INSECURE_REGISTRIES", raising=False)
    side_effect = head if isinstance(head, Exception) else head * 2
    delivery = controllers._Delivery(image=f"{REPOSITORY}:1.2.0")
    controllers._deliveries[KEY] = delivery
    with (
        patch("pubgrade_sidecar.controllers.requests.head", side_effect=side_effect),
        patch("pubgrade_sidecar.controllers.requests.get", return_value=get),
        caplog.at_level(logging.DEBUG),
    ):
        controllers._deliver(delivery, REPOSITORY, "1.2.0", "tesk-api")
    checks = [message for message in caplog.messages if message.startswith("Check ")]
    assert checks == [
        f"Check {n} of 2 for image {REPOSITORY}:1.2.0 failed: {reason}" for n in (1, 2)
    ]
    assert delivery.state == "failed"
    assert delivery.detail == (
        f"Image not available, or its registry not reachable: {reason}."
    )
    for secret in ("secret-token", "registry-password", "robot", "hunter2", "user:"):
        assert secret not in caplog.text
        assert secret not in str(delivery.detail)
