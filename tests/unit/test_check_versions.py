"""Tests of the version check of releases (`scripts/check_versions.py`)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_versions.py"

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location("check_versions", SCRIPT)
assert _spec and _spec.loader
check_versions = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check_versions
_spec.loader.exec_module(check_versions)

PYPROJECT = """\
[build-system]
version = "9.9.9"

[project]
name = "pubgrade-sidecar"
version = "{version}"

[tool.other]
version = "8.8.8"
"""

CHART = """\
apiVersion: v2
name: pubgrade-sidecar
# Set by the release-prep pull request
version: {chart_version}
appVersion: "{version}"  # the default image tag
"""


def _repo(tmp_path: Path, version: str, chart_version: str, app: str) -> Path:
    """Return a repository with the given versions.

    Args:
        tmp_path: Directory to write the files to.
        version: Version in `pyproject.toml`.
        chart_version: `version` of the chart.
        app: `appVersion` of the chart.

    Returns:
        The root of the repository.
    """
    (tmp_path / "deployment").mkdir()
    (tmp_path / "pyproject.toml").write_text(PYPROJECT.format(version=version))
    (tmp_path / "deployment/Chart.yaml").write_text(
        CHART.format(chart_version=chart_version, version=app)
    )
    return tmp_path


def test_repository_versions_are_consistent() -> None:
    """The repository's own files have one version for app and chart."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    chart = (ROOT / "deployment/Chart.yaml").read_text(encoding="utf-8")
    version = check_versions.project_version(pyproject)
    assert version is not None
    assert check_versions.chart_value(chart, "appVersion") == version
    assert check_versions.chart_value(chart, "version") is not None


def test_project_version() -> None:
    """Only `version` of `[project]` counts."""
    text = PYPROJECT.format(version="1.2.3")
    assert check_versions.project_version(text) == "1.2.3"


@pytest.mark.parametrize(
    "text", ['[tool.x]\nversion = "1.0.0"\n', '[project]\nname = "x"\n']
)
def test_project_version_missing(text: str) -> None:
    """Without `[project]` or its `version`, there is none."""
    assert check_versions.project_version(text) is None


def test_chart_value() -> None:
    """Plain and quoted values, with comments."""
    text = CHART.format(chart_version="1.2.3-rc1", version="1.2.3rc1")
    assert check_versions.chart_value(text, "version") == "1.2.3-rc1"
    assert check_versions.chart_value(text, "appVersion") == "1.2.3rc1"
    assert check_versions.chart_value(text, "kubeVersion") is None


def test_main_match(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Matching versions pass."""
    root = _repo(tmp_path, "1.2.3rc1", "1.2.3-rc1", "1.2.3rc1")
    assert check_versions.main(["1.2.3rc1", "1.2.3-rc1"], root) == 0
    assert capsys.readouterr().err == ""


def test_main_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Each mismatch is reported as an error annotation."""
    root = _repo(tmp_path, "1.2.2", "1.2.3", "1.2.4")
    assert check_versions.main(["1.2.3", "1.2.3"], root) == 1
    err = capsys.readouterr().err
    assert "::error::pyproject.toml: [project] version is '1.2.2'" in err
    assert "::error::deployment/Chart.yaml: appVersion is '1.2.4'" in err
    assert "Chart.yaml: version" not in err
