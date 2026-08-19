"""PoseTestBot Flask application factory."""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, send_from_directory

from posetestbot.jobs.runner import LocalJobRunner
from posetestbot.web.routes.monitoring import monitoring_bp
from posetestbot.web.routes.jobs_commands import jobs_commands_bp
from posetestbot.web.routes.lifecycle import lifecycle_bp
from posetestbot.web.routes.system_status import system_status_bp
from posetestbot.web.routes.capture import capture_bp
from posetestbot.web.routes.orchestration import orchestration_bp
from posetestbot.web.routes.sync_quality import sync_quality_bp
from posetestbot.web.routes.calibration import calibration_bp
from posetestbot.web.routes.calibration_library import calibration_library_bp
from posetestbot.web.routes.calibration_targets import calibration_targets_bp
from posetestbot.web.routes.bop_annotations import bop_annotations_bp
from posetestbot.web.routes.bop_evaluation import bop_evaluation_bp
from posetestbot.web.routes.cluster import cluster_bp
from posetestbot.web.routes.overview import overview_bp
from posetestbot.web.routes.pages import pages_bp
from posetestbot.web.routes.pose_templates import pose_templates_bp
from posetestbot.web.routes.run_folders import run_folders_bp
from posetestbot.web.routes.sensors import sensors_bp
from posetestbot.web.routes.ui import ui_bp
from posetestbot.web.routes.workpieces import workpieces_bp
from posetestbot.web.security import install_request_security
from posetestbot.web.runtime import (
    RUNTIME_EXTENSION_KEY,
    WebRuntime,
    WebSettings,
    create_web_runtime,
    default_web_runtime,
)


BRAND_ASSET_DIR = Path(__file__).resolve().parent / "static"
CELL_ASSET_DIR = BRAND_ASSET_DIR / "cell"


class _PreviewPollLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        successful_poll = message.rstrip().endswith('" 200 -')
        noisy_preview_get = any(
            marker in message for marker in ('"GET /sensors/previews',)
        )
        return not (successful_poll and noisy_preview_get)


def _install_preview_poll_log_filter() -> None:
    logger = logging.getLogger("werkzeug")
    if any(isinstance(item, _PreviewPollLogFilter) for item in logger.filters):
        return
    logger.addFilter(_PreviewPollLogFilter())


def create_app(
    *,
    runtime: WebRuntime | None = None,
    job_runner: LocalJobRunner | None = None,
    settings: WebSettings | None = None,
) -> Flask:
    if runtime is not None and (job_runner is not None or settings is not None):
        raise ValueError("runtime cannot be combined with job_runner or settings")
    selected_runtime = (
        runtime
        if runtime is not None
        else (
            create_web_runtime(settings=settings, job_runner=job_runner)
            if job_runner is not None or settings is not None
            else default_web_runtime()
        )
    )
    app = Flask(
        __name__,
        static_folder="static",
        static_url_path="/static",
    )
    app.extensions[RUNTIME_EXTENSION_KEY] = selected_runtime
    install_request_security(app)

    @app.get("/assets/cow_dark.png", defaults={"filename": "cow_dark.png"})
    @app.get("/assets/cow_light.png", defaults={"filename": "cow_light.png"})
    @app.get(
        "/assets/cow_favicon.png",
        defaults={"filename": "cow_favicon.png"},
    )
    def brand_asset(filename: str):
        return send_from_directory(
            BRAND_ASSET_DIR,
            filename,
            max_age=86400,
            mimetype="image/png",
        )

    @app.get("/assets/cell/template_HRI_LBR_all_center_v2.svg")
    def hri_cell_template():
        return send_from_directory(
            CELL_ASSET_DIR,
            "template_HRI_LBR_all_center_v2.svg",
            max_age=86400,
            mimetype="image/svg+xml",
            conditional=True,
        )

    app.register_blueprint(pages_bp)
    app.register_blueprint(jobs_commands_bp)
    app.register_blueprint(lifecycle_bp)
    app.register_blueprint(system_status_bp)
    app.register_blueprint(capture_bp)
    app.register_blueprint(orchestration_bp)
    app.register_blueprint(sync_quality_bp)
    app.register_blueprint(calibration_bp)
    app.register_blueprint(calibration_library_bp)
    app.register_blueprint(calibration_targets_bp)
    app.register_blueprint(bop_annotations_bp)
    app.register_blueprint(bop_evaluation_bp)
    app.register_blueprint(cluster_bp)
    app.register_blueprint(workpieces_bp)
    app.register_blueprint(pose_templates_bp)
    app.register_blueprint(sensors_bp)
    app.register_blueprint(monitoring_bp)
    app.register_blueprint(overview_bp)
    app.register_blueprint(ui_bp)
    app.register_blueprint(run_folders_bp)
    _install_preview_poll_log_filter()
    return app


app = create_app()


if __name__ == "__main__":
    from posetestbot.web.cli import run_web_server

    run_web_server(app)
