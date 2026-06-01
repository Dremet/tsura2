"""Shared extensions and helpers (DB pool, CSRF, etc.)."""

from __future__ import annotations

import atexit
import hashlib
import hmac

from flask import current_app, g
from psycopg_pool import ConnectionPool


class PsycopgPool:
    """Thin wrapper around psycopg‑pool's `ConnectionPool` for Flask apps."""

    def __init__(self) -> None:
        self.pool: ConnectionPool | None = None

    # ------------------------------------------------------------------
    # Flask integration API
    # ------------------------------------------------------------------
    def init_app(self, app):
        dsn = app.config["DATABASE_URL"]
        self.pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=10, timeout=30)

        @app.teardown_appcontext
        def _release_conn(_: Exception | None = None):
            conn = g.pop("db_conn", None)
            if conn is not None and self.pool is not None:
                self.pool.putconn(conn)

        atexit.register(self._close_pool)

    def _close_pool(self):
        if self.pool is not None and not self.pool.closed:
            self.pool.close()

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------
    def get_conn(self):
        if "db_conn" not in g:
            g.db_conn = self.pool.getconn()  # type: ignore[arg-type]
        return g.db_conn


db_pool = PsycopgPool()


def make_csrf_token(session_id: str, secret_key: str) -> str:
    """Return a 64-char HMAC-SHA256 hex token tied to session_id."""
    return hmac.new(
        secret_key.encode(), session_id.encode(), hashlib.sha256
    ).hexdigest()
