"""Shared test setup."""

import os

# Importing the controllers loads the Kubernetes configuration, which a pod
# requires. This runs before the test modules are imported, so that the tests
# can also be collected in a pod, e.g., on a CI runner in a cluster
os.environ.pop("KUBERNETES_SERVICE_HOST", None)
