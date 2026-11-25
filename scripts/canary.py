"""Check that a canary release rolled out, for the workflow `Canary`.

The workflow (`.github/workflows/canary.yml`) pushes a new canary image
(`canary/Dockerfile`), notifies Pubgrade, and then runs this script:

- `rollout`: polls `/version.json` of each canary site until it reports the
  new version, and measures the gap per site, from the notification to the
  first answer with the new version.
- `deliveries`: reads the build at Pubgrade until its notifications are done,
  and checks that every subscription got it (`delivered`), one per site.
- `check`: checks the inputs, before anything is notified.

Each command writes a table to the job summary (the file in
`GITHUB_STEP_SUMMARY`, if set), prints errors as GitHub annotations to
standard error, and exits with 1 if the check fails. See `docs/canary.md`.

Usage:
    `canary.py rollout --sites <JSON list of URLs> --version <X.Y.Z>
    --since <epoch seconds> [--timeout 600] [--interval 10]`
    `canary.py deliveries --url <Pubgrade URL> --repository <id> --build <id>
    --sites <JSON list of URLs> [--timeout 120] [--interval 5]`
    `canary.py check --sites <JSON list of URLs> --url <Pubgrade URL>
    --repository <id>`

Only the stdlib is used, so the workflow runs it without installing the app.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from http.client import HTTPException
import json
import os
import re
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Largest answer that is read, in bytes
MAX_RESPONSE = 64 * 1024
REQUEST_TIMEOUT = 10
# Versions of the canary, as the workflow tags them
# (`1.0.<run number * 100 + attempt>`)
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
# Identifiers of Pubgrade (repositories, builds), which go into URL paths
IDENTIFIER = re.compile(r"[A-Za-z0-9._-]{1,64}")
# Client errors that are worth another try (timeout, rate limit)
RETRIED = frozenset({408, 429})


class CanaryError(Exception):
    """Raised when an input or an answer makes the check impossible."""


class _NoRedirect(HTTPRedirectHandler):
    """Refuse redirects: a site must answer for itself, from its own URL."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        """Follow no redirect, so that the 3xx answer is returned as is.

        Args:
            *args: Arguments of the redirect (unused).
            **kwargs: Keyword arguments of the redirect (unused).
        """


# Opener without redirects (`urlopen` follows them, also to other hosts)
OPENER = build_opener(_NoRedirect)


def schemes(allow_http: bool) -> tuple[str, ...]:
    """Name the URL schemes that may be used.

    Args:
        allow_http: Whether plain HTTP is allowed (local tests only).

    Returns:
        `https`, and `http` if allowed.
    """
    return ("https", "http") if allow_http else ("https",)


def check_url(url: str, allow_http: bool, what: str) -> str:
    """Check a base URL that paths are appended to.

    Args:
        url: The URL.
        allow_http: Whether plain HTTP is allowed (local tests only).
        what: What the URL is of, for the errors.

    Returns:
        The URL without a trailing slash.

    Raises:
        CanaryError: If it can't be parsed, isn't HTTPS (unless allowed), has
            no host, or has credentials, a query or a fragment (also empty
            ones).
    """
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError as exc:
        raise CanaryError(f"{what} isn't a valid URL: {url!r}") from exc
    if parts.scheme not in schemes(allow_http) or not hostname:
        raise CanaryError(f"{what} must be an HTTPS URL: {url!r}")
    if parts.username or parts.password:
        raise CanaryError(f"{what} must not contain credentials.")
    if "?" in url or "#" in url:
        raise CanaryError(f"{what} must not have a query or fragment: {url!r}")
    return url.rstrip("/")


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
        base = check_url(site, allow_http, "A site")
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


def fetch_json(url: str, timeout: float = REQUEST_TIMEOUT) -> tuple[int, Any]:
    """Get a JSON document, without following redirects.

    Args:
        url: URL of the document.
        timeout: Seconds to wait for the server.

    Returns:
        The status and the parsed body, or None as the body if it isn't JSON
        or is larger than `MAX_RESPONSE`. A redirect is returned as its 3xx
        status.

    Raises:
        URLError: If the server can't be reached (also if its host name is
            invalid) or its answer isn't valid HTTP.
    """
    request = Request(url, headers={"Accept": "application/json"})  # noqa: S310
    try:
        with OPENER.open(request, timeout=timeout) as response:
            status = response.status
            content = response.read(MAX_RESPONSE + 1)
    except HTTPError as exc:
        return exc.code, None
    except (HTTPException, UnicodeError) as exc:
        raise URLError(type(exc).__name__) from exc
    if len(content) > MAX_RESPONSE:
        return status, None
    try:
        return status, json.loads(content)
    except (ValueError, RecursionError):
        return status, None


def _request_timeout(remaining: float) -> float:
    """Limit a request to the time that is left, but give it a second.

    Args:
        remaining: Seconds left until the deadline.

    Returns:
        Seconds to wait for the server.
    """
    return min(REQUEST_TIMEOUT, max(remaining, 1.0))


def _seen(site: str, version: str, timeout: float = REQUEST_TIMEOUT) -> bool:
    """Ask a canary site which version it serves.

    Args:
        site: Base URL of the site.
        version: The version to wait for.
        timeout: Seconds to wait for the site.

    Returns:
        Whether the site serves the version; prints why not otherwise.
    """
    try:
        status, body = fetch_json(f"{site}/version.json", timeout)
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
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, float | None]:
    """Wait until every site serves a version, or the time is up.

    Args:
        sites: Base URLs of the sites.
        version: The version to wait for.
        since: Time of the notification (epoch seconds), from which the gaps
            are measured.
        timeout: Seconds to wait in all, from now.
        interval: Seconds between rounds of requests.
        clock: Source of the time (default: `time.time`).
        sleep: Function that waits (default: `time.sleep`).

    Returns:
        Per site, the seconds from `since` to the first answer with the
        version, or None if it never came.
    """
    clock = clock or time.time
    sleep = sleep or time.sleep
    deadline = clock() + timeout
    gaps: dict[str, float | None] = dict.fromkeys(sites)
    while True:
        for site in sites:
            if gaps[site] is None and _seen(
                site, version, _request_timeout(deadline - clock())
            ):
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
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[dict[str, Any]]:
    """Wait until Pubgrade has notified every subscription of a build.

    Args:
        url: Base URL of Pubgrade.
        repository: Identifier of the repository at Pubgrade.
        build: Identifier of the build.
        timeout: Seconds to wait in all.
        interval: Seconds between requests.
        clock: Source of the time (default: `time.time`).
        sleep: Function that waits (default: `time.sleep`).

    Returns:
        The results of the notifications (`notifications` of the build).

    Raises:
        CanaryError: At once if Pubgrade answers with a client error (4xx,
            except 408 and 429);
            otherwise if it can't be reached, answers with a server error or
            the notifications don't finish before the time is up (each of
            these is retried until then).
    """
    target = (
        f"{url.rstrip('/')}/repositories/{check_identifier(repository, 'repository')}"
        f"/builds/{check_identifier(build, 'build')}"
    )
    clock = clock or time.time
    sleep = sleep or time.sleep
    deadline = clock() + timeout
    while True:
        try:
            status, body = fetch_json(target, _request_timeout(deadline - clock()))
        except (URLError, OSError) as exc:
            problem = f"Pubgrade not reachable ({type(exc).__name__})."
        else:
            if 400 <= status < 500 and status not in RETRIED:
                raise CanaryError(f"Pubgrade answered {status} for the build.")
            if (
                status == 200
                and isinstance(body, dict)
                and body.get("finished_at")
                and isinstance(body.get("notifications"), list)
            ):
                return [
                    item for item in body["notifications"] if isinstance(item, dict)
                ]
            problem = (
                f"The notifications didn't finish in {timeout:g} s."
                if status == 200
                else f"Pubgrade answered {status} for the build."
            )
        remaining = deadline - clock()
        if remaining <= 0:
            raise CanaryError(problem)
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
    url = check_url(args.url, args.allow_http, "Pubgrade's URL")
    expected = len(parse_sites(args.sites, allow_http=args.allow_http))
    notifications = wait_for_deliveries(
        url, args.repository, args.build, args.timeout, args.interval
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
    if len(notifications) != expected:
        _error(
            f"Pubgrade notified {len(notifications)} subscriptions, but"
            f" {expected} sites are checked: subscriptions and CANARY_SITES"
            " must match, one per site."
        )
        return 1
    return 1 if failed else 0


def _check(args: argparse.Namespace) -> int:
    """Run the command `check`: check the inputs before anything is notified.

    Args:
        args: Parsed arguments.

    Returns:
        Exit status.

    Raises:
        CanaryError: If an input is invalid.
    """
    parse_sites(args.sites, allow_http=args.allow_http)
    check_url(args.url, args.allow_http, "Pubgrade's URL")
    check_identifier(args.repository, "repository")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command.

    Args:
        argv: Arguments, without the program name.

    Returns:
        Exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--allow-http", action="store_true", help="allow plain HTTP (local tests)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    rollout = commands.add_parser(
        "rollout", parents=[common], help="wait for the sites"
    )
    rollout.add_argument("--sites", required=True)
    rollout.add_argument("--version", required=True)
    rollout.add_argument("--since", required=True, type=float)
    rollout.add_argument("--timeout", type=float, default=600)
    rollout.add_argument("--interval", type=float, default=10)
    deliveries = commands.add_parser(
        "deliveries", parents=[common], help="check Pubgrade's results"
    )
    deliveries.add_argument("--url", required=True)
    deliveries.add_argument("--repository", required=True)
    deliveries.add_argument("--build", required=True)
    deliveries.add_argument("--sites", required=True)
    deliveries.add_argument("--timeout", type=float, default=120)
    deliveries.add_argument("--interval", type=float, default=5)
    check = commands.add_parser(
        "check", parents=[common], help="check the inputs before notifying"
    )
    check.add_argument("--sites", required=True)
    check.add_argument("--url", required=True)
    check.add_argument("--repository", required=True)
    args = parser.parse_args(argv)
    run = {"rollout": _rollout, "deliveries": _deliveries, "check": _check}[
        args.command
    ]
    try:
        return run(args)
    except (CanaryError, URLError, OSError) as exc:
        _error(str(exc) if isinstance(exc, CanaryError) else type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
