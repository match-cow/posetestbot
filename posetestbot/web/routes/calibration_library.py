"""Operator calibration-library and immutable selection endpoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from posetestbot.calibration.profile_library import (
    CalibrationSelectionConflict,
    list_calibration_library,
    select_calibration_profile_composite_snapshot,
    select_calibration_profile_snapshot,
)


calibration_library_bp = Blueprint("calibration_library", __name__)


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
