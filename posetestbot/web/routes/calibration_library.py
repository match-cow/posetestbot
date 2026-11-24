"""Operator calibration-library and immutable selection endpoints."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify, request

from posetestbot.calibration.profile_library import (
    CalibrationSelectionConflict,
    list_calibration_library,
    select_calibration_profile_composite_snapshot,
    select_calibration_profile_snapshot,
)
from posetestbot.calibration.retained_library import (
    calibration_library_overview,
    delete_retained_calibration,
    retain_calibration,
)
from posetestbot.run_folders import resolve_direct_run_folder
from posetestbot.web.security import require_run_folder_target, web_run_roots


calibration_library_bp = Blueprint("calibration_library", __name__)


@calibration_library_bp.get("/ui/calibrations/library")
def calibration_library_view():
    """Every calibration on disk, independent of any destination run."""

    try:
        return jsonify(calibration_library_overview())
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400


@calibration_library_bp.post("/ui/calibrations/library/retain")
def retain_library_calibration():
    """Copy a calibration a run still holds into the retained library."""

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"output": "JSON object required"}), 400
    unsupported = sorted(set(data) - {"run_root", "bundle_sha256"})
    if unsupported:
        return (
            jsonify({"output": "Unsupported fields: " + ", ".join(unsupported)}),
            400,
        )
    try:
        if not isinstance(data.get("run_root"), str) or not data["run_root"]:
            raise ValueError("run_root is required")
        run = resolve_direct_run_folder(
            data["run_root"], allowed_roots=web_run_roots()
        )
        result = retain_calibration(
            run, expected_bundle_sha256=data.get("bundle_sha256")
        )
    except LookupError as exc:
        return jsonify({"output": str(exc)}), 409
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return jsonify({"output": str(exc)}), 400
    return jsonify(result), 201


@calibration_library_bp.delete("/ui/calibrations/library/<bundle_sha256>")
def delete_library_calibration(bundle_sha256: str):
    """Permanently remove one retained entry; run snapshots are untouched."""

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("confirm") is not True:
        return (
            jsonify(
                {"output": "confirm must be literal true to delete a calibration"}
            ),
            400,
        )
    try:
        return jsonify(delete_retained_calibration(bundle_sha256))
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400


@calibration_library_bp.get("/ui/calibrations")
def calibration_library():
    run_root = request.args.get("run_root")
    if not run_root:
        return jsonify({"output": "run_root is required"}), 400
    try:
        return jsonify(list_calibration_library(run_root))
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400


def _select_calibration_response():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"output": "JSON object required"}), 400
    if not data.get("run_root"):
        return jsonify({"output": "run_root is required"}), 400
    try:
        require_run_folder_target(Path(data["run_root"]))
        common_fields = {
            "run_root",
            "sensors",
            "resolution",
            "operator",
            "expected_current_bundle_sha256",
            "confirm_replace",
        }
        selection_fields = (
            {"source_selections"}
            if "source_selections" in data
            else {"source_run_root", "expected_bundle_sha256"}
        )
        unsupported = sorted(set(data) - common_fields - selection_fields)
        if unsupported:
            raise ValueError(
                "Calibration selection contains unsupported fields: "
                + ", ".join(unsupported)
            )
        common = {
            "sensors": data.get("sensors"),
            "resolution": data.get("resolution"),
            "operator": data.get("operator"),
            "expected_current_bundle_sha256": data.get(
                "expected_current_bundle_sha256"
            ),
            "confirm_replace": data.get("confirm_replace", False),
        }
        if "source_selections" in data:
            result = select_calibration_profile_composite_snapshot(
                data["run_root"],
                source_selections=data["source_selections"],
                **common,
            )
        else:
            if not data.get("source_run_root"):
                return jsonify({"output": "source_run_root is required"}), 400
            if not data.get("expected_bundle_sha256"):
                return jsonify({"output": "expected_bundle_sha256 is required"}), 400
            result = select_calibration_profile_snapshot(
                data["run_root"],
                source_run_root=data["source_run_root"],
                expected_bundle_sha256=data["expected_bundle_sha256"],
                **common,
            )
    except CalibrationSelectionConflict as exc:
        return jsonify({"output": str(exc), "issues": exc.issues}), 409
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400
    return jsonify(result), 201


@calibration_library_bp.post("/ui/calibrations/select")
def select_calibration():
    return _select_calibration_response()
