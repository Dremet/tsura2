"""Application factory and component registration."""

from __future__ import annotations

import os

import psycopg.rows
from dotenv import load_dotenv
from flask import Flask, g, request

from .extensions import db_pool, make_csrf_token
from .blueprints.main import main_bp
from .blueprints.auth import auth_bp


def create_app() -> Flask:
    load_dotenv()

    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates",
    )

    db_url = os.environ.get("TSU_HOTLAPPING_POSTGRES_URL")
    if not db_url:
        raise RuntimeError("TSU_HOTLAPPING_POSTGRES_URL is not set")
    app.config["DATABASE_URL"] = db_url

    secret_key = os.environ.get("TSURA_SECRET_KEY")
    if not secret_key:
        raise RuntimeError("TSURA_SECRET_KEY is not set")
    app.config["SECRET_KEY"] = secret_key

    app.config["TSURA_BASE_URL"] = (
        os.environ.get("TSURA_BASE_URL", "http://localhost:5000").rstrip("/")
    )

    db_pool.init_app(app)

    @app.before_request
    def _load_session():
        g.current_steam_id = None
        g.session_id = None
        sid = request.cookies.get("tsura_sid")
        if not sid:
            return
        try:
            conn = db_pool.get_conn()
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    "SELECT steam_id FROM mart.user_sessions"
                    " WHERE session_id = %s AND expires_at > now()",
                    (sid,),
                )
                row = cur.fetchone()
            if row:
                g.current_steam_id = row["steam_id"]
                g.session_id = sid
        except Exception:
            pass

    @app.context_processor
    def _inject_auth():
        sid = g.get("session_id")
        csrf = make_csrf_token(sid, app.config["SECRET_KEY"]) if sid else None
        return {
            "current_steam_id": g.get("current_steam_id"),
            "csrf_token": csrf,
        }

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)

    return app
