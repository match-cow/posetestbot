from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from posetestbot.calibration.intrinsics import factory_intrinsic_profile
from posetestbot.calibration.rectification import (
    PROVENANCE_SCHEMA_VERSION,
    RECTIFICATION_PROVENANCE,
    rectify_run,
    validate_rectification_provenance,
)
from posetestbot.io.artifacts import (
    CAMERA_RECTIFICATION_REPORT,
    DEPTH_DIR,
    FRAME_METADATA_JSONL,
    MATCH_ROBOT_EE_POSES,
    RGB_DIR,
)
from posetestbot.pipeline.run_config import (
    SensorRunConfig,
    create_run_config,
    write_run_config,
)


def digest_tree(path: Path) -> dict[str, str]:
    return {
        item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def rectification_fixture(run_root: Path) -> tuple[Path, dict]:
    write_run_config(
        run_root,
        create_run_config(
            run_root=run_root,
            capture_intent="dataset",
            bop_annotation_mode="none",
            sensors=(
                SensorRunConfig("realsense_d435", "SERIAL-1", "Rectification fixture"),
            ),
        ),
    )
    sensor = run_root / "processed" / "synchronized" / "realsense_SERIAL-1"
    (sensor / "rgb").mkdir(parents=True)
    (sensor / "depth").mkdir()
    width, height = 20, 16
    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    rgb[:, :, 0] = np.arange(width, dtype=np.uint8)
    depth = np.arange(width * height, dtype=np.uint16).reshape(height, width) + 1
    depth[0, 0] = 0
    assert cv2.imwrite((sensor / "rgb" / "000000.png").as_posix(), rgb)
    assert cv2.imwrite((sensor / "depth" / "000000.png").as_posix(), depth)
    (sensor / "cam_K.txt").write_text(
        "25 0 10\n0 25 8\n0 0 1\n0.2 -0.05 0.01 -0.01 0.0\n"
    )
    (sensor / "depthscale.txt").write_text("1.0\n")
    (sensor / "camera_data.json").write_text(
        json.dumps(
            {"K": [[25, 0, 10], [0, 25, 8], [0, 0, 1]], "resolution": [height, width]}
        )
    )
    (sensor / "frame_metadata.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": "SERIAL-1",
                "orientation": "normal",
                "frame_index": 0,
                "frame_id": "000000.png",
                "rgb_path": "rgb/000000.png",
                "depth_path": "depth/000000.png",
                "host_received_timestamp_ns": 123,
                "host_wall_timestamp_ns": 123,
                "sensor_timestamp_ns": 100,
            }
        )
        + "\n"
    )
    (sensor / MATCH_ROBOT_EE_POSES).write_text(
        json.dumps(
            {
                "000000.png": {
                    "robot_ee_pose": {"X": 1, "Y": 2, "Z": 3, "A": 0, "B": 0, "C": 0}
                }
            }
        )
    )
    return sensor, factory_intrinsic_profile(sensor)


def test_rectification_is_transactional_non_destructive_and_depth_nearest(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    sensor, profile = rectification_fixture(run_root)
    before = digest_tree(sensor)
    source_depth = cv2.imread(
        (sensor / "depth" / "000000.png").as_posix(), cv2.IMREAD_UNCHANGED
    )

    report_path, report = rectify_run(run_root, [profile])

    assert report_path == run_root / CAMERA_RECTIFICATION_REPORT
    assert report["mode"] == "managed_canonical"
    assert report["sensor_count"] == 1
    assert digest_tree(sensor) == before
    output = run_root / "processed" / "rectified" / sensor.name
    output_depth = cv2.imread(
        (output / "depth" / "000000.png").as_posix(), cv2.IMREAD_UNCHANGED
    )
    assert set(np.unique(output_depth)).issubset({*np.unique(source_depth), 0})
    assert (output / MATCH_ROBOT_EE_POSES).read_bytes() == (
        sensor / MATCH_ROBOT_EE_POSES
    ).read_bytes()
    metadata = json.loads((output / "frame_metadata.jsonl").read_text())
    source_metadata = json.loads((sensor / "frame_metadata.jsonl").read_text())
    assert {key: value for key, value in metadata.items() if key != "derivation"} == (
        source_metadata
    )
    assert metadata["frame_id"] == "000000.png"
    assert metadata["rgb_path"] == "rgb/000000.png"
    assert metadata["depth_path"] == "depth/000000.png"
    assert metadata["host_received_timestamp_ns"] == 123
    assert metadata["sensor_timestamp_ns"] == 100
    assert metadata["derivation"]["depth_interpolation"] == "nearest"
    assert metadata["derivation"]["intrinsic_profile_id"] == profile["profile_id"]
    cam_k_lines = (output / "cam_K.txt").read_text().splitlines()
    assert cam_k_lines[3] == "0.0 0.0 0.0 0.0 0.0"
    camera_data = json.loads((output / "camera_data.json").read_text())
    assert camera_data["projection"] == "rectified_alpha0"
    assert camera_data["distortion"] == [0.0] * 5
    provenance = validate_rectification_provenance(sensor, output)
    assert provenance["schema_version"] == PROVENANCE_SCHEMA_VERSION
    assert (
        provenance["source_fingerprint"] == report["sensors"][0]["source_fingerprint"]
    )
    assert (
        provenance["output_fingerprint"] == report["sensors"][0]["output_fingerprint"]
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("grayscale_rgb", r"shape \(height, width, 3\|4\)"),
        ("uint16_rgb", "rgb_image must use uint8"),
        ("uint8_depth", "depth_image must use uint16"),
        ("multichannel_depth", "depth_image must have shape"),
    ],
)
def test_rectification_rejects_invalid_rgbd_pixel_contract_before_remap(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    run_root = tmp_path / "run"
    sensor, profile = rectification_fixture(run_root)
    rgb_path = sensor / RGB_DIR / "000000.png"
    depth_path = sensor / DEPTH_DIR / "000000.png"
    if mutation == "grayscale_rgb":
        replacement = np.zeros((16, 20), dtype=np.uint8)
        assert cv2.imwrite(rgb_path.as_posix(), replacement)
    elif mutation == "uint16_rgb":
        replacement = np.zeros((16, 20, 3), dtype=np.uint16)
        assert cv2.imwrite(rgb_path.as_posix(), replacement)
    elif mutation == "uint8_depth":
        replacement = np.zeros((16, 20), dtype=np.uint8)
        assert cv2.imwrite(depth_path.as_posix(), replacement)
    else:
        replacement = np.zeros((16, 20, 3), dtype=np.uint16)
        assert cv2.imwrite(depth_path.as_posix(), replacement)

    with pytest.raises(ValueError, match=message):
        rectify_run(run_root, [profile])

    assert not (run_root / "processed" / "rectified").exists()
    assert not (run_root / CAMERA_RECTIFICATION_REPORT).exists()


def test_rectification_rejects_corrupt_staged_png_after_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    _sensor, profile = rectification_fixture(run_root)
    original_imwrite = cv2.imwrite
    corrupted = False

    def corrupt_first_staged_rgb(path: str, image: np.ndarray) -> bool:
        nonlocal corrupted
        result = original_imwrite(path, image)
        output = Path(path)
        if not corrupted and output.parent.name == RGB_DIR:
            output.write_bytes(b"not-a-png")
            corrupted = True
        return result

    monkeypatch.setattr(cv2, "imwrite", corrupt_first_staged_rgb)

    with pytest.raises(ValueError, match="Unreadable RGB-D frame pair"):
        rectify_run(run_root, [profile])

    assert corrupted is True
    assert not (run_root / "processed" / "rectified").exists()
    assert not (run_root / CAMERA_RECTIFICATION_REPORT).exists()


@pytest.mark.parametrize(
    ("mutated_tree", "message"),
    [
        ("synchronized", "source fingerprint"),
        ("rectified", "output fingerprint"),
    ],
)
def test_rectification_provenance_detects_stale_source_or_mutated_output(
    tmp_path: Path,
    mutated_tree: str,
    message: str,
) -> None:
    run_root = tmp_path / "run"
    sensor, profile = rectification_fixture(run_root)
    rectify_run(run_root, [profile])
    output = run_root / "processed" / "rectified" / sensor.name
    target = (
        (sensor if mutated_tree == "synchronized" else output) / "rgb" / "000000.png"
    )
    target.write_bytes(target.read_bytes() + b"mutated")

    with pytest.raises(ValueError, match=message):
        validate_rectification_provenance(sensor, output)

    assert (output / RECTIFICATION_PROVENANCE).is_file()


def test_rectification_refuses_profile_orientation_mismatch(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    _sensor, profile = rectification_fixture(run_root)
    profile = {**profile, "orientation": "inverted"}

    with pytest.raises(ValueError, match="exactly one"):
        rectify_run(run_root, [profile])

    assert not (run_root / "processed" / "rectified").exists()


@pytest.mark.parametrize(
    "artifact",
    [FRAME_METADATA_JSONL, MATCH_ROBOT_EE_POSES],
)
def test_rectification_requires_synchronized_frame_binding_sidecars(
    tmp_path: Path,
    artifact: str,
) -> None:
    run_root = tmp_path / "run"
    sensor, profile = rectification_fixture(run_root)
    (sensor / artifact).unlink()

    with pytest.raises(ValueError, match="Camera artifact must be a regular file"):
        rectify_run(run_root, [profile])

    assert not (run_root / "processed" / "rectified").exists()
    assert not (run_root / CAMERA_RECTIFICATION_REPORT).exists()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rgb_path", "rgb/not-the-frame.png", "rgb_path must be"),
        ("depth_path", "depth/not-the-frame.png", "depth_path must be"),
    ],
)
def test_rectification_rejects_frame_metadata_not_bound_to_rgbd(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    run_root = tmp_path / "run"
    sensor, profile = rectification_fixture(run_root)
    metadata_path = sensor / FRAME_METADATA_JSONL
    metadata = json.loads(metadata_path.read_text())
    metadata[field] = value
    metadata_path.write_text(json.dumps(metadata) + "\n")

    with pytest.raises(ValueError, match=message):
        rectify_run(run_root, [profile])

    assert not (run_root / "processed" / "rectified").exists()


def test_rectification_rejects_frame_metadata_with_extra_frame_binding(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    sensor, profile = rectification_fixture(run_root)
    metadata_path = sensor / FRAME_METADATA_JSONL
    metadata = json.loads(metadata_path.read_text())
    metadata.update(
        {
            "frame_id": "999999.png",
            "rgb_path": "rgb/999999.png",
            "depth_path": "depth/999999.png",
        }
    )
    metadata_path.write_text(json.dumps(metadata) + "\n")

    with pytest.raises(ValueError, match="must cover exactly"):
        rectify_run(run_root, [profile])

    assert not (run_root / "processed" / "rectified").exists()


@pytest.mark.parametrize("mutation", ["coverage", "pose"])
def test_rectification_rejects_invalid_matched_robot_pose_bindings(
    tmp_path: Path,
    mutation: str,
) -> None:
    run_root = tmp_path / "run"
    sensor, profile = rectification_fixture(run_root)
    matched_path = sensor / MATCH_ROBOT_EE_POSES
    matched = json.loads(matched_path.read_text())
    if mutation == "coverage":
        matched["999999.png"] = matched["000000.png"]
        message = "must cover exactly"
    else:
        matched["000000.png"]["robot_ee_pose"]["C"] = None
        message = "field C must be finite"
    matched_path.write_text(json.dumps(matched))

    with pytest.raises(ValueError, match=message):
        rectify_run(run_root, [profile])

    assert not (run_root / "processed" / "rectified").exists()


def test_rectification_requires_every_enabled_sensor_folder(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    _sensor, profile = rectification_fixture(run_root)
    write_run_config(
        run_root,
        create_run_config(
            run_root=run_root,
            capture_intent="dataset",
            bop_annotation_mode="none",
            sensors=(
                SensorRunConfig("realsense_d435", "SERIAL-1", "Present"),
                SensorRunConfig("oak_d_pro", "SERIAL-2", "Missing"),
            ),
        ),
    )

    with pytest.raises(
        FileNotFoundError,
        match="luxonis_SERIAL-2",
    ):
        rectify_run(run_root, [profile])

    assert not (run_root / "processed" / "rectified").exists()


def test_custom_rectification_roots_are_explicitly_unmanaged_and_isolated(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    sensor, profile = rectification_fixture(run_root)
    diagnostic_output = run_root / "processed" / "diagnostics" / "rectified"

    with pytest.raises(ValueError, match="diagnostic-only"):
        rectify_run(
            run_root,
            [profile],
            input_root=sensor.parent,
            output_root=diagnostic_output,
        )
    with pytest.raises(ValueError, match="must not write the canonical"):
        rectify_run(
            run_root,
            [profile],
            input_root=sensor.parent,
            output_root=run_root / "processed" / "rectified",
            diagnostic_unmanaged=True,
        )

    report_path, report = rectify_run(
        run_root,
        [profile],
        input_root=sensor.parent,
        output_root=diagnostic_output,
        diagnostic_unmanaged=True,
    )

    assert report["mode"] == "diagnostic_unmanaged"
    assert report_path == diagnostic_output / CAMERA_RECTIFICATION_REPORT
    assert not (run_root / CAMERA_RECTIFICATION_REPORT).exists()
    assert not (run_root / "processed" / "rectified").exists()


@pytest.mark.parametrize(
    "output_factory",
    [
        lambda run_root, _sensor: run_root,
        lambda _run_root, sensor: sensor.parent,
        lambda _run_root, sensor: sensor / "derived",
    ],
)
def test_diagnostic_rectification_rejects_destructive_path_overlap(
    tmp_path: Path,
    output_factory,
) -> None:
    run_root = tmp_path / "run"
    sensor, profile = rectification_fixture(run_root)
    raw_sentinel = sensor / RGB_DIR / "000000.png"
    raw_before = raw_sentinel.read_bytes()
    output_root = output_factory(run_root, sensor)

    with pytest.raises(ValueError, match="different|overlap|contain the run root"):
        rectify_run(
            run_root,
            [profile],
            input_root=sensor.parent,
            output_root=output_root,
            diagnostic_unmanaged=True,
            overwrite=True,
        )

    assert raw_sentinel.read_bytes() == raw_before
    assert (run_root / "run_config.json").is_file()


def test_diagnostic_rectification_never_overwrites_existing_output(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    sensor, profile = rectification_fixture(run_root)
    output_root = run_root / "processed" / "diagnostics" / "rectified"
    output_root.mkdir(parents=True)
    sentinel = output_root / "diagnostic-sentinel.txt"
    sentinel.write_text("preserve me\n")

    with pytest.raises(ValueError, match="does not overwrite"):
        rectify_run(
            run_root,
            [profile],
            input_root=sensor.parent,
            output_root=output_root,
            diagnostic_unmanaged=True,
            overwrite=True,
        )

    assert sentinel.read_text() == "preserve me\n"


def test_rectification_refuses_unavailable_forward_projection(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    _sensor, profile = rectification_fixture(run_root)
    profile = {
        **profile,
        "native": {
            **profile["native"],
            "distortion_model": "inverse_brown_conrady",
        },
        "rectified": None,
        "source": {
            **profile["source"],
            "opencv_projection_compatible": False,
            "rectification_available": False,
            "rectification_unavailable_reason": (
                "sdk_distortion_model_is_not_forward_opencv_compatible"
            ),
        },
    }

    with pytest.raises(
        ValueError,
        match="no OpenCV-compatible rectified projection",
    ):
        rectify_run(run_root, [profile])

    assert not (run_root / "processed" / "rectified").exists()
