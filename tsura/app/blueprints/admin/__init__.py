"""Admin blueprint: cross-server admin area (config panels + admin rights)."""
from flask import Blueprint, g, redirect, request, url_for

admin_bp = Blueprint(
    "admin", __name__, url_prefix="/admin", template_folder="templates"
)


@admin_bp.before_request
def _login_before_admin():
    if request.endpoint and not g.get("current_steam_id"):
        destination = (request.full_path if request.query_string else request.path)
        if request.method != "GET":
            destination = url_for("admin.index")
        return redirect(url_for("auth.login", next=destination))


from . import routes  # noqa: E402,F401
from . import calendar  # noqa: E402,F401
