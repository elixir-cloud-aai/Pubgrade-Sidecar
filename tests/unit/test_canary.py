"""Tests of the canary's rollout checks (`scripts/canary.py`)."""

from __future__ import annotations

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading
from typing import Any, ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/canary.py"

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location("canary", SCRIPT)
assert _spec and _spec.loader
canary = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = canary
_spec.loader.exec_module(canary)


class Stub(BaseHTTPRequestHandler):
    """Stub of canary sites and of Pubgrade: answers from `routes`.

    Each route maps a path to a list of answers (status, body), served in
    order; the last one repeats.
    """

    routes: ClassVar[dict[str, list[tuple[int, Any]]]] = {}
    calls: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        """Answer a GET from the routes."""
        self.calls.append(self.path)
        answers = self.routes.get(self.path, [(404, {"message": "none"})])
        status, body = answers.pop(0) if len(answers) > 1 else answers[0]
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test output quiet.

        Args:
            format: Format of the message.
            *args: Values of the message.
        """


@pytest.fixture
def stub() -> Iterator[str]:
    """Start the stub server on a free port.

    Yields:
        Its base URL.
    """
    Stub.routes = {}
    Stub.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


class Clock:
    """Fake clock: `sleep` advances `time`."""

    def __init__(self, start: float = 1000.0) -> None:
        """Start the clock.

        Args:
            start: Time at the start, in seconds.
        """
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        """Tell the time.

        Returns:
            The current time.
        """
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the clock.

        Args:
            seconds: Seconds to advance.
        """
        self.sleeps.append(seconds)
        self.now += seconds


def version(value: str) -> tuple[int, dict[str, str]]:
    """Build an answer of a canary's `/version.json`.

    Args:
        value: Version that the canary reports.

    Returns:
        Status and body.
    """
    return 200, {"version": value, "commit": "abc1234"}


@pytest.mark.parametrize(
    "text,message",
    [
        ("", "JSON"),
        ("{}", "list"),
        ("[]", "at least one"),
        ('["https://a.example.org", "https://a.example.org/"]', "twice"),
        ('["http://a.example.org"]', "HTTPS"),
        ('["https://a.example.org?x=1"]', "query"),
        ('["https://user:pw@a.example.org"]', "credentials"),
        ("[1]", "URL"),
    ],
)
def test_invalid_sites(text: str, message: str) -> None:
    """Test that a list of sites that can't work is refused, with a reason."""
    with pytest.raises(canary.CanaryError, match=message):
        canary.parse_sites(text)


def test_sites_parsed() -> None:
    """Test that sites are taken without a trailing slash."""
    sites = canary.parse_sites('["https://a.example.org/", "https://b.example.org/x"]')
    assert sites == ["https://a.example.org", "https://b.example.org/x"]


def test_http_only_when_allowed() -> None:
    """Test that HTTP sites are taken only if allowed (local tests)."""
    assert canary.parse_sites('["http://127.0.0.1:1"]', allow_http=True) == [
        "http://127.0.0.1:1"
    ]


@pytest.mark.parametrize("value", ["", "a/b", "../x", "x" * 65, "ab cd"])
def test_invalid_identifier(value: str) -> None:
    """Test that identifiers that could change the URL path are refused."""
    with pytest.raises(canary.CanaryError, match="identifier"):
        canary.check_identifier(value, "build")


def test_rollout_seen(stub: str) -> None:
    """Test that the gap per site is measured until each shows the version."""
    Stub.routes = {
        "/a/version.json": [version("1.0.1"), version("1.0.1"), version("1.0.2")],
        "/b/version.json": [version("1.0.2")],
    }
    clock = Clock()
    gaps = canary.wait_for_version(
        [f"{stub}/a", f"{stub}/b"],
        "1.0.2",
        since=990.0,
        timeout=60,
        interval=10,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert gaps == {f"{stub}/a": 30.0, f"{stub}/b": 10.0}
    assert clock.sleeps == [10, 10]


def test_rollout_times_out(stub: str) -> None:
    """Test that a site that never shows the version has no gap."""
    Stub.routes = {"/a/version.json": [version("1.0.1")]}
    clock = Clock()
    gaps = canary.wait_for_version(
        [f"{stub}/a"],
        "1.0.2",
        since=1000.0,
        timeout=25,
        interval=10,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert gaps == {f"{stub}/a": None}
    assert clock.sleeps == [10, 10, 5]


@pytest.mark.parametrize(
    "answers",
    [
        [(503, {"message": "rolling"})],
        [(200, b"not json")],
        [(200, ["no", "object"])],
        [(200, b"x" * (canary.MAX_RESPONSE + 1))],
    ],
)
def test_bad_answers_are_retried(
    stub: str, answers: list[tuple[int, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Test that a site that answers badly for a while still counts later."""
    Stub.routes = {"/a/version.json": [*answers, version("1.0.2")]}
    clock = Clock()
    gaps = canary.wait_for_version(
        [f"{stub}/a"],
        "1.0.2",
        since=1000.0,
        timeout=60,
        interval=10,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert gaps == {f"{stub}/a": 10.0}
    assert "not yet" in capsys.readouterr().out


def test_unreachable_site(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that a site that can't be reached is retried, then has no gap."""
    clock = Clock()
    gaps = canary.wait_for_version(
        ["http://127.0.0.1:9"],
        "1.0.2",
        since=1000.0,
        timeout=10,
        interval=10,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert gaps == {"http://127.0.0.1:9": None}
    assert "URLError" in capsys.readouterr().out


def build(results: list[str] | None, finished: bool = True) -> dict[str, Any]:
    """Build a build record of Pubgrade.

    Args:
        results: Results of the notifications, or None for none yet.
        finished: Whether the build has finished.

    Returns:
        The record.
    """
    record: dict[str, Any] = {
        "status": "SUCCEEDED" if finished else "QUEUED",
        "started_at": "2026-11-25T06:00:00Z",
        "finished_at": "2026-11-25T06:00:05Z" if finished else None,
    }
    if results is not None:
        record["notifications"] = [
            {
                "subscription_id": f"s{index}",
                "result": result,
                "http_status": 202 if result == "delivered" else 500,
                "finished_at": "2026-11-25T06:00:05Z",
            }
            for index, result in enumerate(results)
        ]
    return record


def test_deliveries_waited_for(stub: str) -> None:
    """Test that the notifications are read once they are done."""
    Stub.routes = {
        "/repositories/r1/builds/b1": [
            (200, build(None, finished=False)),
            (200, build(["delivered", "delivered"])),
        ]
    }
    clock = Clock()
    notifications = canary.wait_for_deliveries(
        stub, "r1", "b1", timeout=60, interval=5, clock=clock.time, sleep=clock.sleep
    )
    assert [item["result"] for item in notifications] == ["delivered", "delivered"]
    assert clock.sleeps == [5]


def test_deliveries_time_out(stub: str) -> None:
    """Test that notifications that never finish are an error."""
    Stub.routes = {"/repositories/r1/builds/b1": [(200, build(None, finished=False))]}
    clock = Clock()
    with pytest.raises(canary.CanaryError, match="didn't finish"):
        canary.wait_for_deliveries(
            stub,
            "r1",
            "b1",
            timeout=10,
            interval=5,
            clock=clock.time,
            sleep=clock.sleep,
        )


def test_deliveries_of_missing_build(stub: str) -> None:
    """Test that a missing build is an error at once."""
    clock = Clock()
    with pytest.raises(canary.CanaryError, match="404"):
        canary.wait_for_deliveries(
            stub,
            "r1",
            "b1",
            timeout=60,
            interval=5,
            clock=clock.time,
            sleep=clock.sleep,
        )


def run_main(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, str]:
    """Run the script's entry point with a job summary file.

    Args:
        argv: Arguments.
        tmp_path: Directory for the summary.
        monkeypatch: Fixture for changing the environment.

    Returns:
        Exit status and the summary.
    """
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(canary.time, "sleep", lambda _: None)
    status = canary.main(argv)
    return status, summary.read_text() if summary.exists() else ""


def test_main_rollout_passes(
    stub: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that a rollout seen at every site passes, with the gaps."""
    Stub.routes = {"/a/version.json": [version("1.0.7")]}
    status, summary = run_main(
        [
            "rollout",
            "--sites",
            json.dumps([f"{stub}/a"]),
            "--version",
            "1.0.7",
            "--since",
            "0",
            "--allow-http",
        ],
        tmp_path,
        monkeypatch,
    )
    assert status == 0
    assert f"| {stub}/a | 1.0.7 |" in summary


def test_main_rollout_fails(
    stub: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test that a site without the version fails the step, with an error."""
    Stub.routes = {"/a/version.json": [version("1.0.6")]}
    status, summary = run_main(
        [
            "rollout",
            "--sites",
            json.dumps([f"{stub}/a"]),
            "--version",
            "1.0.7",
            "--since",
            "0",
            "--timeout",
            "0",
            "--allow-http",
        ],
        tmp_path,
        monkeypatch,
    )
    assert status == 1
    assert "not rolled out" in summary
    assert "::error::" in capsys.readouterr().err


@pytest.mark.parametrize(
    "results,expected",
    [(["delivered"], 0), (["delivered", "failed"], 1), (["skipped"], 1), ([], 1)],
)
def test_main_deliveries(
    stub: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    results: list[str],
    expected: int,
) -> None:
    """Test that only notifications that were all delivered pass."""
    Stub.routes = {"/repositories/r1/builds/b1": [(200, build(results))]}
    status, summary = run_main(
        [
            "deliveries",
            "--url",
            stub,
            "--repository",
            "r1",
            "--build",
            "b1",
            "--allow-http",
        ],
        tmp_path,
        monkeypatch,
    )
    assert status == expected
    for index, result in enumerate(results):
        assert f"| s{index} | {result} |" in summary


def test_main_input_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test that invalid inputs fail with an annotation, not a traceback."""
    status, _ = run_main(
        ["rollout", "--sites", "[]", "--version", "1.0.7", "--since", "0"],
        tmp_path,
        monkeypatch,
    )
    assert status == 1
    assert "::error::" in capsys.readouterr().err


def test_main_without_summary(stub: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that the script also runs outside GitHub Actions."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    Stub.routes = {"/a/version.json": [version("1.0.7")]}
    assert (
        canary.main(
            [
                "rollout",
                "--sites",
                json.dumps([f"{stub}/a"]),
                "--version",
                "1.0.7",
                "--since",
                "0",
                "--allow-http",
            ]
        )
        == 0
    )


def test_invalid_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that a version that isn't X.Y.Z is refused."""
    status, _ = run_main(
        [
            "rollout",
            "--sites",
            '["https://a.example.org"]',
            "--version",
            "latest",
            "--since",
            "0",
        ],
        tmp_path,
        monkeypatch,
    )
    assert status == 1


def test_pubgrade_over_http_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test that Pubgrade is asked over HTTPS only, unless allowed."""
    status, _ = run_main(
        [
            "deliveries",
            "--url",
            "http://pubgrade.example.org",
            "--repository",
            "r1",
            "--build",
            "b1",
        ],
        tmp_path,
        monkeypatch,
    )
    assert status == 1
    assert "HTTPS" in capsys.readouterr().err
