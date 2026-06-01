"""Steam OpenID 2.0 authentication: login, callback, logout."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import psycopg
import requests
from flask import current_app, g, make_response, redirect, request, url_for

from . import auth_bp
from ...extensions import db_pool


_STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
_CLAIMED_ID_RE    = re.compile(r"https://steamcommunity\.com/openid/id/(\d+)$")
_SESSION_DAYS     = 30


def _verify_openid(params: dict) -> int | None:
    """POST to Steam to verify the OpenID assertion server-side.

    Never trusts the claimed steam_id from the client; verification is
    performed via a check_authentication round-trip to Steam's servers.
    Returns the verified steam_id (int) or None on failure.
    """
    check_params = dict(params)
    check_params["openid.mode"] = "check_authentication"
    try:
        resp = requests.post(_STEAM_OPENID_URL, data=check_params, timeout=5)
        if "is_valid:true" not in resp.text:
            return None
    except Exception:
        return None
    m = _CLAIMED_ID_RE.match(params.get("openid.claimed_id", ""))
    return int(m.group(1)) if m else None


@auth_bp.route("/login")
def login():
    base_url = current_app.config["TSURA_BASE_URL"]
    params = {
        "openid.ns":         "http://specs.openid.net/auth/2.0",
        "openid.mode":       "checkid_setup",
        "openid.return_to":  f"{base_url}/auth/callback",
        "openid.realm":      base_url,
        "openid.identity":   "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return redirect(f"{_STEAM_OPENID_URL}?{urlencode(params)}")


@auth_bp.route("/callback")
def callback():
    steam_id = _verify_openid(request.args.to_dict())
    if not steam_id:
        return redirect(url_for("main.index"))

    session_id = secrets.token_hex(32)
    expires    = datetime.now(timezone.utc) + timedelta(days=_SESSION_DAYS)

    conn = db_pool.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM mart.user_sessions WHERE steam_id = %s AND expires_at < now()",
            (steam_id,),
        )
        cur.execute(
            "INSERT INTO mart.user_sessions (session_id, steam_id, expires_at)"
            " VALUES (%s, %s, %s)",
            (session_id, steam_id, expires),
        )
    conn.commit()

    base_url     = current_app.config["TSURA_BASE_URL"]
    secure_cookie = base_url.startswith("https://")

    resp = make_response(redirect(url_for("main.index")))
    resp.set_cookie(
        "tsura_sid",
        session_id,
        max_age=_SESSION_DAYS * 86400,
        httponly=True,
        secure=secure_cookie,
        samesite="Lax",
    )
    return resp


@auth_bp.route("/logout")
def logout():
    sid = request.cookies.get("tsura_sid")
    if sid:
        conn = db_pool.get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM mart.user_sessions WHERE session_id = %s", (sid,)
            )
        conn.commit()

    resp = make_response(redirect(url_for("main.index")))
    resp.set_cookie("tsura_sid", "", max_age=0, httponly=True, samesite="Lax")
    return resp
