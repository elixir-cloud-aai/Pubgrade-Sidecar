#!/usr/bin/env python3
"""Start the sidecar with the development server of Flask."""

from foca import Foca

if __name__ == "__main__":
    foca = Foca(config_file="config/config.yaml")
    app = foca.create_app()
    app.run()
