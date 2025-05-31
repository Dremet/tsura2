"""Application factory and component registration."""

from __future__ import annotations

import os
from flask import Flask
from dotenv import load_dotenv

from .extensions import db_pool
from .blueprints.main import main_bp


def create_app() -> Flask:
    """Create and configure a Flask app instance."""
    load_dotenv()  # Read variables from .env into the environment

    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates",
    )

    # Database DSN is read‑only; we connect via a connection pool
    app.config["DATABASE_URL"] = os.environ.get("TSU_HOTLAPPING_POSTGRES_URL")
    if not app.config["DATABASE_URL"]:
        raise RuntimeError("TSU_HOTLAPPING_POSTGRES_URL is not set")

    db_pool.init_app(app)

    # Register Blueprints (one per feature module)
    app.register_blueprint(main_bp)

    return app
