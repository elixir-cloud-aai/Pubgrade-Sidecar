"""Check that a canary release rolled out, for the workflow `Canary`.

The workflow (`.github/workflows/canary.yml`) pushes a new canary image
(`canary/Dockerfile`), notifies Pubgrade, and then runs this script:

- `rollout`: polls `/version.json` of each canary site until it reports the
  new version, and measures the gap per site, from the notification to the
  first answer with the new version.
- `deliveries`: reads the build at Pubgrade until its notifications are done,
  and checks that every subscription got it (`delivered`).

Each command writes a table to the job summary (the file in
`GITHUB_STEP_SUMMARY`, if set), prints errors as GitHub annotations to
standard error, and exits with 1 if the check fails. See `docs/canary.md`.

Usage:
    `canary.py rollout --sites <JSON list of URLs> --version <X.Y.Z>
    --since <epoch seconds> [--timeout 600] [--interval 10]`
    `canary.py deliveries --url <Pubgrade URL> --repository <id> --build <id>
    [--timeout 120] [--interval 5]`

Only the stdlib is used, so the workflow runs it without installing the app.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
import os
import re
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

# Largest answer that is read, in bytes
MAX_RESPONSE = 64 * 1024
REQUEST_TIMEOUT = 10
# Versions of the canary, as the workflow tags them (`1.0.<run number>`)
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
# Identifiers of Pubgrade (repositories, builds), which go into URL paths
IDENTIFIER = re.compile(r"[A-Za-z0-9._-]{1,64}")


class CanaryError(Exception):
    """Raised when an input or an answer makes the check impossible."""


def parse_sites(text: str, allow_http: bool = False) -> list[str]:
    """Read the list of canary sites.

    Args:
        text: JSON list of base URLs, e.g., `["https://canary.example.org"]`.
        allow_http: Whether plain HTTP is allowed (local tests only).

    Returns:
        The base URLs, without a trailing slash.

    Raises:
        CanaryError: If the list is invalid, empty, has duplicates, or a URL
            isn't HTTPS (unless allowed), or has credentials, a query or a
            fragment.
    """
    try:
        sites = json.loads(text)
    except ValueError as exc:
        raise CanaryError("The sites aren't JSON.") from exc
    if not isinstance(sites, list):
        raise CanaryError("The sites must be a JSON list of URLs.")
    if not sites:
        raise CanaryError("The sites must name at least one URL.")
    result: list[str] = []
    for site in sites:
        if not isinstance(site, str):
            raise CanaryError("Each site must be a URL (a string).")
        parts = urlsplit(site)
        schemes = ("https", "http") if allow_http else ("https",)
        if parts.scheme not in schemes or not parts.hostname:
            raise CanaryError(f"Sites must be HTTPS URLs: {site!r}")
        if parts.username or parts.password:
            raise CanaryError("Site URLs must not contain credentials.")
        if parts.query or parts.fragment:
            raise CanaryError(f"Site URLs must not have a query or fragment: {site!r}")
        base = site.rstrip("/")
        if base in result:
            raise CanaryError(f"A site is listed twice: {base!r}")
        result.append(base)
    return result


def check_identifier(value: str, name: str) -> str:
    """Check an identifier of Pubgrade that goes into a URL path.

    Args:
        value: The identifier.
        name: What it identifies, for the error.

    Returns:
        The identifier.

    Raises:
        CanaryError: If it isn't 1 to 64 letters, digits, `.`, `_` or `-`.
    """
    if not IDENTIFIER.fullmatch(value) or value in (".", ".."):
        raise CanaryError(f"Not a valid {name} identifier.")
    return value


def fetch_json(url: str) -> tuple[int, Any]:
    """Get a JSON document.

    Args:
        url: URL of the document.

    Returns:
        The status and the parsed body, or None as the body if it isn't JSON
        or is larger than `MAX_RESPONSE`.

    Raises:
        URLError: If the server can't be reached.
    """
    request = Request(url, headers={"Accept": "application/json"})  # noqa: S310
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310
            status = response.status
            content = response.read(MAX_RESPONSE + 1)
    except HTTPError as exc:
        return exc.code, None
    if len(content) > MAX_RESPONSE:
        return status, None
    try:
        return status, json.loads(content)
    except ValueError:
        return status, None


def _seen(site: str, version: str) -> bool:
    """Ask a canary site which version it serves.

    Args:
        site: Base URL of the site.
        version: The version to wait for.

    Returns:
        Whether the site serves the version; prints why not otherwise.
    """
    try:
        status, body = fetch_json(f"{site}/version.json")
    except (URLError, OSError) as exc:
        print(f"{site}: not yet ({type(exc).__name__})")
        return False
    served = body.get("version") if isinstance(body, dict) else None
    if status == 200 and served == version:
        return True
    print(f"{site}: not yet (status {status}, version {served!r})")
    return False


def wait_for_version(
    sites: Sequence[str],
    version: str,
    since: float,
    timeout: float,
    interval: float,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, float | None]:
    """Wait until every site serves a version, or the time is up.

    Args:
        sites: Base URLs of the sites.
        version: The version to wait for.
        since: Time of the notification (epoch seconds), from which the gaps
            are measured.
        timeout: Seconds to wait in all, from now.
        interval: Seconds between rounds of requests.
        clock: Source of the time.
        sleep: Function that waits.

    Returns:
        Per site, the seconds from `since` to the first answer with the
        version, or None if it never came.
    """
    deadline = clock() + timeout
    gaps: dict[str, float | None] = dict.fromkeys(sites)
    while True:
        for site in sites:
            if gaps[site] is None and _seen(site, version):
                gaps[site] = clock() - since
        remaining = deadline - clock()
        if all(gap is not None for gap in gaps.values()) or remaining <= 0:
            return gaps
        sleep(min(interval, remaining))


def wait_for_deliveries(
    url: str,
    repository: str,
    build: str,
    timeout: float,
    interval: float,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Wait until Pubgrade has notified every subscription of a build.

    Args:
        url: Base URL of Pubgrade.
        repository: Identifier of the repository at Pubgrade.
        build: Identifier of the build.
        timeout: Seconds to wait in all.
        interval: Seconds between requests.
        clock: Source of the time.
        sleep: Function that waits.

    Returns:
        The results of the notifications (`notifications` of the build).

    Raises:
        CanaryError: If Pubgrade answers other than 200, or the notifications
            don't finish in time.
        URLError: If Pubgrade can't be reached.
    """
    target = (
        f"{url.rstrip('/')}/repositories/{check_identifier(repository, 'repository')}"
        f"/builds/{check_identifier(build, 'build')}"
    )
    deadline = clock() + timeout
    while True:
        status, body = fetch_json(target)
        if status != 200:
            raise CanaryError(f"Pubgrade answered {status} for the build.")
        if (
            isinstance(body, dict)
            and body.get("finished_at")
            and isinstance(body.get("notifications"), list)
        ):
            return [item for item in body["notifications"] if isinstance(item, dict)]
        remaining = deadline - clock()
        if remaining <= 0:
            raise CanaryError(f"The notifications didn't finish in {timeout:g} s.")
        sleep(min(interval, remaining))


def _summary(lines: Sequence[str]) -> None:
    """Append lines to the job summary, if there is one.

    Args:
        lines: Markdown lines.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    print(text)
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(text)


def _error(message: str) -> None:
    """Print an error as a GitHub annotation.

    Args:
        message: The error.
    """
    print(f"::error::{message}", file=sys.stderr)


def _rollout(args: argparse.Namespace) -> int:
    """Run the command `rollout`.

    Args:
        args: Parsed arguments.

    Returns:
        Exit status.

    Raises:
        CanaryError: If an input is invalid.
    """
    sites = parse_sites(args.sites, allow_http=args.allow_http)
    if not VERSION.fullmatch(args.version):
        raise CanaryError("The version must be X.Y.Z.")
    gaps = wait_for_version(
        sites, args.version, args.since, args.timeout, args.interval
    )
    lines = [
        "### Canary rollout",
        "",
        "| Site | Version | Gap |",
        "| --- | --- | --- |",
    ]
    for site, gap in gaps.items():
        if gap is None:
            lines.append(f"| {site} | not rolled out within {args.timeout:g} s | - |")
        else:
            lines.append(f"| {site} | {args.version} | {gap:.0f} s |")
    _summary(lines)
    missing = [site for site, gap in gaps.items() if gap is None]
    for site in missing:
        _error(f"{site} didn't serve {args.version} within {args.timeout:g} s.")
    return 1 if missing else 0


def _deliveries(args: argparse.Namespace) -> int:
    """Run the command `deliveries`.

    Args:
        args: Parsed arguments.

    Returns:
        Exit status.

    Raises:
        CanaryError: If an input is invalid or Pubgrade's answer unusable.
    """
    parts = urlsplit(args.url)
    if parts.scheme not in (("https", "http") if args.allow_http else ("https",)):
        raise CanaryError("Pubgrade's URL must be HTTPS.")
    notifications = wait_for_deliveries(
        args.url, args.repository, args.build, args.timeout, args.interval
    )
    lines = [
        "### Pubgrade notifications",
        "",
        "| Subscription | Result | HTTP status |",
        "| --- | --- | --- |",
    ]
    for item in notifications:
        lines.append(
            f"| {item.get('subscription_id')} | {item.get('result')} "
            f"| {item.get('http_status')} |"
        )
    _summary(lines)
    if not notifications:
        _error("Pubgrade notified no subscription of the canary build.")
        return 1
    failed = [item for item in notifications if item.get("result") != "delivered"]
    for item in failed:
        _error(
            f"Subscription {item.get('subscription_id')}: {item.get('result')}"
            f" (HTTP status {item.get('http_status')})."
        )
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command.

    Args:
        argv: Arguments, without the program name.

    Returns:
        Exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    rollout = commands.add_parser("rollout", help="wait for the sites")
    rollout.add_argument("--sites", required=True)
    rollout.add_argument("--version", required=True)
    rollout.add_argument("--since", required=True, type=float)
    rollout.add_argument("--timeout", type=float, default=600)
    rollout.add_argument("--interval", type=float, default=10)
    rollout.add_argument("--allow-http", action="store_true")
    deliveries = commands.add_parser("deliveries", help="check Pubgrade's results")
    deliveries.add_argument("--url", required=True)
    deliveries.add_argument("--repository", required=True)
    deliveries.add_argument("--build", required=True)
    deliveries.add_argument("--timeout", type=float, default=120)
    deliveries.add_argument("--interval", type=float, default=5)
    deliveries.add_argument("--allow-http", action="store_true")
    args = parser.parse_args(argv)
    run = _rollout if args.command == "rollout" else _deliveries
    try:
        return run(args)
    except (CanaryError, URLError, OSError) as exc:
        _error(str(exc) if isinstance(exc, CanaryError) else type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
