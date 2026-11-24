"""Run-owned synchronization quality report API."""

from flask import Blueprint

from posetestbot.web import route_support


sync_quality_bp = Blueprint("sync_quality", __name__)


@sync_quality_bp.get("/sync/quality")
def sync_quality_endpoint():
    return route_support.sync_quality_endpoint()
