"""Read-only runtime, robot-profile, sensor-adapter, and hardware evidence APIs."""

from flask import Blueprint

from posetestbot.web import route_support


system_status_bp = Blueprint("system_status", __name__)


@system_status_bp.get("/sensors/adapters")
def sensor_adapters():
    return route_support.sensor_adapters()


@system_status_bp.get("/runtime/status")
def runtime_status():
    return route_support.runtime_status()


@system_status_bp.get("/robot/status")
def robot_status():
    return route_support.robot_status()


@system_status_bp.get("/hardware/status")
def hardware_status():
    return route_support.hardware_status()
