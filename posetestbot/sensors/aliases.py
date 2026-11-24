"""Lab-local aliases and UI defaults for discovered sensors."""

from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
from typing import Any, Mapping

from posetestbot.io.atomic import atomic_write_json
from posetestbot.pipeline.run_config import normalize_inverted, normalize_mounting_mode
from posetestbot.sensors.contracts import SensorType


def _default_sensor_aliases_path() -> Path:
    explicit_root = os.environ.get("POSETESTBOT_APP_ROOT")
    if explicit_root:
        app_root = Path(explicit_root).expanduser().resolve()
    else:
        source_root = Path(__file__).resolve().parents[2]
        app_root = (
            source_root
            if (source_root / "pyproject.toml").is_file()
            else Path.cwd()
        )
    return app_root / "working_data" / "sensor_aliases.json"


DEFAULT_SENSOR_ALIASES_PATH = _default_sensor_aliases_path()


def sensor_alias_key(sensor_type: SensorType | str, device_id: str) -> str:
    normalized = sensor_type if isinstance(sensor_type, SensorType) else SensorType(sensor_type)
    return f"{normalized.value}:{str(device_id).strip()}"


def normalize_sensor_alias_record(value: Mapping[str, Any]) -> dict[str, Any]:
    alias = str(value.get("alias", "")).strip()
    record: dict[str, Any] = {"alias": alias}
    if value.get("mounting_mode") not in {None, ""}:
        record["mounting_mode"] = normalize_mounting_mode(
            str(value["mounting_mode"])
        ).value
    if value.get("inverted") not in {None, ""}:
        record["inverted"] = normalize_inverted(value["inverted"])
    return record


def normalize_sensor_aliases(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    aliases: dict[str, dict[str, Any]] = {}
    for key, record in value.items():
        if not isinstance(record, Mapping):
            raise ValueError(f"Alias record for {key!r} must be a JSON object")
        sensor_type_value, separator, device_id = str(key).partition(":")
        if not separator or not device_id.strip():
            raise ValueError(f"Alias key must look like sensor_type:device_id, got {key!r}")
        alias_key = sensor_alias_key(SensorType(sensor_type_value), device_id)
        aliases[alias_key] = normalize_sensor_alias_record(record)
    return aliases


def load_sensor_aliases(
    path: str | Path = DEFAULT_SENSOR_ALIASES_PATH,
    *,
    tolerate_errors: bool = True,
) -> dict[str, dict[str, Any]]:
    alias_path = Path(path)
    if not alias_path.is_file():
        return {}
    try:
        with open(alias_path, "r") as f:
            value = json.load(f)
        if not isinstance(value, Mapping):
            raise ValueError("Sensor aliases file must contain a JSON object")
        aliases_value = value.get("aliases", value)
        if not isinstance(aliases_value, Mapping):
            raise ValueError("Sensor aliases must be a JSON object")
        return normalize_sensor_aliases(aliases_value)
    except Exception:
        if tolerate_errors:
            return {}
        raise


def save_sensor_aliases(
    aliases: Mapping[str, Any],
    path: str | Path = DEFAULT_SENSOR_ALIASES_PATH,
    *,
    merge: bool = False,
) -> Path:
    alias_path = Path(path)
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    lock = alias_path.with_name(f".{alias_path.name}.lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if merge:
            current = load_sensor_aliases(alias_path, tolerate_errors=False)
            for raw_key, fields in aliases.items():
                if not isinstance(fields, Mapping) or set(fields) - {"alias", "mounting_mode", "inverted"}:
                    raise ValueError("Unsupported device default fields")
                key = next(iter(normalize_sensor_aliases({raw_key: fields})))
                current[key] = {**current.get(key, {}), **fields}
            aliases = current
        return atomic_write_json(alias_path, normalize_sensor_aliases(aliases))
    finally:
        os.close(descriptor)


def sensor_alias_file_state(path: str | Path = DEFAULT_SENSOR_ALIASES_PATH) -> dict[str, Any]:
    alias_path = Path(path)
    try:
        aliases = load_sensor_aliases(alias_path, tolerate_errors=False)
    except Exception as exc:
        return {
            "path": alias_path.as_posix(),
            "aliases": {},
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "path": alias_path.as_posix(),
        "aliases": aliases,
        "error": None,
    }


def alias_record_for_device(
    aliases: Mapping[str, Mapping[str, Any]],
    *,
    sensor_type: SensorType | str,
    device_id: str,
) -> Mapping[str, Any]:
    try:
        key = sensor_alias_key(sensor_type, device_id)
    except ValueError:
        return {}
    record = aliases.get(key, {})
    return record if isinstance(record, Mapping) else {}
