"""Filesystem and scalar validation for the trusted-LAN web API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from werkzeug.datastructures import ImmutableMultiDict
from werkzeug.exceptions import RequestEntityTooLarge

from posetestbot.web.paths import APP_ROOT

DEFAULT_RUN_ROOT = APP_ROOT / "working_data"
DEFAULT_RUN_ROOTS = (DEFAULT_RUN_ROOT, Path("/mnt/working_data_ssd"))
DEFAULT_INPUT_ROOTS = (APP_ROOT / "scripts" / "default_data",)
TRUE_STRINGS = {"1", "true", "yes", "on"}
FALSE_STRINGS = {"0", "false", "no", "off"}
BOOLEAN_FIELDS = {
    "allow_cameras",
    "allow_failed_preflight",
    "allow_missing_preflight",
    "allow_real_robot",
    "allow_stale_preflight",
    "compare_hand_eye_methods",
    "confirm_idle_program_exit",
    "download",
    "from_detected_sensors",
    "include_runtimes",
    "include_sensors",
    "include_terminal",
    "no_gt",
    "no_masks",
    "no_nearest_pose_threshold",
    "no_residual_thresholds",
    "no_runtimes",
    "no_sensors",
    "plan_only",
    "promote",
    "require_valid",
}
EXECUTION_ACKNOWLEDGEMENT_FIELDS = {
    "allow_cameras",
    "allow_real_robot",
    "confirm_idle_program_exit",
}
RUN_PATH_FIELDS = {"candidates", "observations", "profiles", "run_config"}
OUTPUT_PATH_FIELDS = {"output_profiles"}
INPUT_PATH_FIELDS = {
    "calibration_profiles",
    "intrinsic_calibration_profiles",
    "target",
    "target_spec",
    "target_to_reference",
    "target_to_reference_path",
}
CALIBRATION_TARGET_MAX_REQUEST_BYTES = 256 * 1024
CATALOG_AND_TEMPLATE_MAX_JSON_BYTES = 2 * 1024 * 1024


def parse_strict_bool(value: Any, *, name: str, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in TRUE_STRINGS:
            return True
        if normalized in FALSE_STRINGS:
            return False
    raise ValueError(
        f"{name} must be a JSON boolean or one of: true, false, 1, 0, yes, no, on, off"
    )


def _configured_roots(variable: str, defaults: tuple[Path, ...]) -> tuple[Path, ...]:
    roots = [path.resolve() for path in defaults]
    raw = os.environ.get(variable, "")
    for entry in raw.split(os.pathsep):
        if not entry.strip():
            continue
        path = Path(entry).expanduser()
        if not path.is_absolute():
            path = APP_ROOT / path
        resolved = path.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def web_run_roots() -> tuple[Path, ...]:
    return _configured_roots("POSETESTBOT_WEB_RUN_ROOTS", DEFAULT_RUN_ROOTS)


def web_input_roots() -> tuple[Path, ...]:
    return _configured_roots("POSETESTBOT_WEB_INPUT_ROOTS", DEFAULT_INPUT_ROOTS)


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_below(path: Path, roots: tuple[Path, ...], *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not any(_is_below(resolved, root) for root in roots):
        allowed = ", ".join(root.as_posix() for root in roots)
        raise ValueError(f"{label} must remain below an allowed root: {allowed}")
    return resolved


def resolve_web_run_root(value: Any) -> Path:
    if not isinstance(value, str | Path) or not str(value).strip():
        raise ValueError("run_root must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        if path.parts and path.parts[0] == DEFAULT_RUN_ROOT.name:
            path = APP_ROOT / path
        else:
            path = DEFAULT_RUN_ROOT / path
    return _require_below(path, web_run_roots(), label="run_root")


def resolve_web_scoped_path(
    value: Any,
    *,
    scope: str,
    run_root: Path,
    label: str,
) -> Path:
    if not isinstance(value, str | Path) or not str(value).strip():
        raise ValueError(f"{label} must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        if scope == "repository":
            path = APP_ROOT / path
        elif scope == "input" and (APP_ROOT / path).exists():
            path = APP_ROOT / path
        else:
            path = run_root / path
    if scope in {"run", "output"}:
        roots = (run_root.resolve(),)
    elif scope == "input":
        roots = (run_root.resolve(), *web_input_roots())
    elif scope == "repository":
        roots = (APP_ROOT.resolve(),)
    else:
        raise ValueError(f"Unknown web path scope: {scope}")
    return _require_below(path, roots, label=label)


def _normalize_json_payload(data: dict[str, Any]) -> None:
    for key in EXECUTION_ACKNOWLEDGEMENT_FIELDS & data.keys():
        if not isinstance(data[key], bool):
            raise ValueError(f"{key} must be a literal JSON boolean")
    for key in (BOOLEAN_FIELDS - EXECUTION_ACKNOWLEDGEMENT_FIELDS) & data.keys():
        data[key] = parse_strict_bool(data[key], name=key)
    raw_run_root = data.get("run_root")
    if raw_run_root is None:
        return
    run_root = resolve_web_run_root(raw_run_root)
    data["run_root"] = run_root.as_posix()
    for key in RUN_PATH_FIELDS & data.keys():
        if data[key] not in {None, ""}:
            data[key] = resolve_web_scoped_path(
                data[key], scope="run", run_root=run_root, label=key
            ).as_posix()
    for key in OUTPUT_PATH_FIELDS & data.keys():
        if data[key] not in {None, ""}:
            data[key] = resolve_web_scoped_path(
                data[key], scope="output", run_root=run_root, label=key
            ).as_posix()
    for key in INPUT_PATH_FIELDS & data.keys():
        if isinstance(data[key], str) and data[key]:
            data[key] = resolve_web_scoped_path(
                data[key], scope="input", run_root=run_root, label=key
            ).as_posix()


def _normalize_query_arguments() -> None:
    pairs = list(request.args.items(multi=True))
    if not pairs:
        return
    run_root: Path | None = None
    normalized_pairs: list[tuple[str, str]] = []
    for key, value in pairs:
        if key == "run_root":
            run_root = resolve_web_run_root(value)
            value = run_root.as_posix()
        elif key in BOOLEAN_FIELDS:
            value = "true" if parse_strict_bool(value, name=key) else "false"
        normalized_pairs.append((key, value))
    if run_root is not None:
        rewritten = []
        for key, value in normalized_pairs:
            scope = None
            if key in RUN_PATH_FIELDS:
                scope = "run"
            elif key in OUTPUT_PATH_FIELDS:
                scope = "output"
            elif key in INPUT_PATH_FIELDS:
                scope = "input"
            if scope is not None and value:
                value = resolve_web_scoped_path(
                    value, scope=scope, run_root=run_root, label=key
                ).as_posix()
            rewritten.append((key, value))
        normalized_pairs = rewritten
    request.args = ImmutableMultiDict(normalized_pairs)


def install_request_security(app: Flask) -> None:
    @app.before_request
    def validate_request_boundaries():
        request_limit: int | None = None
        limit_message = "Request body exceeds this endpoint's size limit"
        if request.path.startswith("/calibration-targets/"):
            request_limit = CALIBRATION_TARGET_MAX_REQUEST_BYTES
            limit_message = "Calibration-target request exceeds 256 KiB"
        elif request.is_json and request.path.startswith(
            ("/workpieces/", "/pose-templates/")
        ):
            request_limit = CATALOG_AND_TEMPLATE_MAX_JSON_BYTES
            limit_message = "JSON request exceeds 2 MiB"
        if request_limit is not None:
            # This must run before get_json(). Read at most one byte beyond the
            # accepted size so unknown-length WSGI streams cannot be silently
            # truncated into an invalid JSON document and reported as a 400.
            request.max_content_length = request_limit + 1
            if (
                request.content_length is not None
                and request.content_length > request_limit
            ):
                return jsonify({"output": limit_message}), 413
            try:
                if len(request.get_data(cache=True)) > request_limit:
                    return jsonify({"output": limit_message}), 413
            except RequestEntityTooLarge:
                return jsonify({"output": limit_message}), 413
            request.max_content_length = request_limit
        try:
            _normalize_query_arguments()
            data = request.get_json(silent=True)
            if isinstance(data, dict):
                _normalize_json_payload(data)
        except RequestEntityTooLarge:
            return jsonify({"output": limit_message}), 413
        except (TypeError, ValueError) as exc:
            return jsonify({"output": str(exc)}), 400
        return None
