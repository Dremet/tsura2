"""Career blueprint: seasons, car tuning, standings, upgrade overview, admin."""
from flask import Blueprint

career_bp = Blueprint(
    "career", __name__, url_prefix="/career", template_folder="templates"
)

from . import routes  # noqa: E402,F401
