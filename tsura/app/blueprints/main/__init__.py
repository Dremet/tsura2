"""Public‑facing routes (home page and simple views)."""

from flask import Blueprint

main_bp = Blueprint("main", __name__, template_folder="templates")

from . import routes  # noqa: E402  – defer import to avoid circulars
