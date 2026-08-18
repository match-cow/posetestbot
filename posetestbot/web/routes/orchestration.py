"""Purpose-specific configuration and queued workflow APIs."""

from flask import Blueprint

from posetestbot.web import route_support


orchestration_bp = Blueprint("orchestration", __name__)


@orchestration_bp.route("/run-config", methods=["GET", "POST"])
def run_config():
    return route_support.run_config()


@orchestration_bp.post("/preflight/jobs")
def preflight_jobs():
    return route_support.submit_preflight_job()


@orchestration_bp.post("/dataset-processing/jobs")
def dataset_processing_jobs():
    return route_support.submit_dataset_processing_job()


@orchestration_bp.post("/robot/commands")
def robot_commands():
    return route_support.robot_commands()
