"""Durable background-job APIs."""

from flask import Blueprint

from posetestbot.web import route_support


jobs_commands_bp = Blueprint("jobs_commands", __name__)


@jobs_commands_bp.get("/jobs")
def list_jobs():
    return route_support.list_jobs()


@jobs_commands_bp.get("/jobs/<job_id>")
def get_job(job_id: str):
    return route_support.get_job(job_id)


@jobs_commands_bp.get("/jobs/<job_id>/log")
def get_job_log(job_id: str):
    return route_support.get_job_log(job_id)


@jobs_commands_bp.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    return route_support.cancel_job(job_id)
