"""Tests for the image build (`Dockerfile`) and its consistency with the lock."""

from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[2]
BASE_IMAGE = re.compile(
    r"^FROM elixircloud/foca:(?P<version>[^-\s@]+)-py(?P<python>[0-9.]+)@sha256:",
    re.MULTILINE,
)


def _base_image() -> re.Match[str]:
    """Find the base image in the `Dockerfile`.

    Returns:
        Match with the FOCA version and Python version of the base image.
    """
    match = BASE_IMAGE.search((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert match is not None, "No FOCA base image pinned by digest"
    return match


def test_base_image_has_locked_foca() -> None:
    """Test that the base image has the FOCA release of the lock.

    Otherwise the image build would install another FOCA on top of it.
    """
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in lock["package"]}
    assert _base_image()["version"] == locked["foca"]


def test_base_image_has_required_python() -> None:
    """Test that the base image has the Python version the project requires."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required = project["project"]["requires-python"]
    assert required == f"=={_base_image()['python']}.*"


def test_image_serves_app_with_gunicorn() -> None:
    """Test that the image serves the app factory with Gunicorn.

    With FOCA's settings, one worker: deliveries are kept in memory.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(
        r'^CMD \["gunicorn", "--config", "python:foca.gunicorn_config",'
        r'\s*\\?\s*"pubgrade_sidecar.app:create_app\(\)"\]$',
        dockerfile,
        re.MULTILINE,
    )
