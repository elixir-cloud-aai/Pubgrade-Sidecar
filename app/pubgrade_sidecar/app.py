"""App factory, and entry point for development.

The image serves the app with Gunicorn (`pubgrade_sidecar.app:create_app()`,
with FOCA's settings, `foca.gunicorn_config`); `python -m pubgrade_sidecar.app`
(in `app/`) runs it with Flask's development server.
"""

from pathlib import Path

from connexion import App
from foca import Foca

# The configuration of the app, independent of the working directory
CONFIG_FILE = Path(__file__).resolve().parents[1] / "config" / "config.yaml"


def create_app() -> App:
    """Create the app from its configuration.

    Gunicorn calls this in its worker, after forking. The sidecar keeps its
    deliveries in memory, so it must run in a single worker process (as with
    FOCA's Gunicorn settings), with threads.

    Returns:
        Connexion app.
    """
    return Foca(config_file=CONFIG_FILE).create_app()


def main() -> None:
    """Run the app with Flask's development server, on the configured port.

    For development; not for production.
    """
    app = create_app()
    app.run(port=app.port)


if __name__ == "__main__":
    main()
