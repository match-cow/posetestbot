"""Capture planning, execution evidence, and capture-job APIs."""

from flask import Blueprint, request

from posetestbot.web import route_support


capture_bp = Blueprint("capture", __name__)


@capture_bp.route("/capture/jobs", methods=["GET", "POST"])
def list_capture_jobs():
    if request.method == "POST":
        return route_support.submit_capture_job()
    return route_support.list_capture_jobs()


@capture_bp.get("/capture/status")
def capture_execution_status():
    return route_support.capture_execution_status()


@capture_bp.post("/capture/jobs/<job_id>/stop")
def stop_capture_job(job_id: str):
    return route_support.stop_capture_job(job_id)
