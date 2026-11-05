"""Shared test setup."""

import os

import pytest

# Importing the controllers loads the Kubernetes configuration, which a pod
# requires. This runs before the test modules are imported, so that the tests
# can also be collected in a pod, e.g., on a CI runner in a cluster
os.environ.pop("KUBERNETES_SERVICE_HOST", None)


@pytest.fixture(autouse=True)
def access_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure the access token.

    Args:
        monkeypatch: Fixture for changing the environment.

    Returns:
        The access token.
    """
    token = "test-token"
    monkeypatch.setenv("ACCESS_TOKEN", token)
    return token
