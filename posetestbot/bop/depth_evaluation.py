"""Queued Inspect-only depth studies and finish comparisons.

All writes are derived artifacts below processed/bop_evaluation/depth. A
completion directory is renamed into place only after source revalidation.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import uuid
import zipfile

import cv2
import numpy as np
from PIL import Image

from posetestbot.jobs.cancellation import write_terminal_progress
from posetestbot.bop.depth_metrics import (
    REVISION,
    Aggregate,
    ComparisonReport,
    ComparisonSubmission,
    EvaluationReport,
    EvaluationSubmission,
    MetricSettings,
    measure,
    reference_bins,
    regions,
)
from posetestbot.bop.depth_sources import (
    BOP_MANIFEST,
    digest,
    hash_sources,
    identity,
    inventory,
    read_json,
    require_pinhole_projections,
    retained_projections,
    setup,
    verify_hashes,
    verify_identities,
)
from posetestbot.bop.evaluation import _has_symlink_component, _sha256_file
from posetestbot.bop.inspection import (
    _required_plain_file,
    _safe_relative,
    _valid_recorded_run_root,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.sqlite import open_readonly


BASE = Path("processed/bop_evaluation/depth")
APP_ROOT = Path(__file__).resolve().parents[2]
KINDS = {"evaluations": "depth-eval", "comparisons": "depth-compare"}
_UNSPECIFIED_MANIFEST = object()
IMPLEMENTATION_FILES = (
    "posetestbot/bop/depth_evaluation.py",
    "posetestbot/bop/depth_sources.py",
    "posetestbot/bop/depth_metrics.py",
    "scripts/blenderproc_depth_reference.py",
)
ASSUMPTIONS = [
    "Controlled scene without unmodelled foreground occluders.",
    "One finish label per run; calibrated GT poses are the geometric reference.",
    "Signed error = measured optical-axis depth − reference depth (mm). No fitting or measured-depth correction.",
    "Eligibility uses all known opaque objects rendered together, independently of measured depth, visible masks, and BOP19 targets.",
    "Camera-distance bins use Euclidean reference surface range; incidence is the acute angle between the flat normal and viewing ray.",
]


class DepthCanceled(Exception):
    pass


def now():
    return datetime.now(UTC).isoformat()


@lru_cache(maxsize=4)
def _version(executable, mtime):
    return subprocess.run(
        [executable, "-v"], capture_output=True, text=True, check=True, timeout=5
    ).stdout.strip()


def renderer_status():
    executable = shutil.which("blenderproc")
    try:
        version = (
            _version(executable, Path(executable).stat().st_mtime_ns)
            if executable
            else None
        )
    except (OSError, subprocess.SubprocessError):
        # Not cached: one probe that timed out on a busy host must not report
        # the renderer as missing until the web service is restarted.
        version = None
    available = version == "2.8.0"
    return {
        "available": available,
        "version": version,
        "required_version": "2.8.0",
        "reason": None
        if available
        else "Pinned BlenderProc 2.8.0 is required. Run bash scripts/install.sh --with-blenderproc.",
        "device": "CPU",
    }


def folder(root, kind, record_id):
    root = Path(root).resolve()
    if (
        kind not in KINDS
        or not isinstance(record_id, str)
        or not re.fullmatch(KINDS[kind] + r"-[0-9a-f]{12}", record_id)
    ):
        raise KeyError("Unknown depth record")
    path = root / BASE / kind / record_id
    if _has_symlink_component(path, root=root):
        raise ValueError("Depth artifact path must not use symbolic links")
    return path


def _request(root, kind, record_id):
    path = folder(root, kind, record_id)
    value = read_json(
        Path(root).resolve(),
        (path / "request.json").relative_to(Path(root).resolve()).as_posix(),
    )
    if (
        value.get("schema_version")
        != f"depth_{'evaluation' if kind == 'evaluations' else 'comparison'}_request.v1"
        or value.get("id") != record_id
        or not _valid_recorded_run_root(value.get("run_root"))
    ):
        raise ValueError("Depth request identity/schema is invalid")
    return value


def _store_request(root, kind, value):
    path = folder(root, kind, value["id"])
    path.mkdir(parents=True, exist_ok=False)
    atomic_write_json(path / "request.json", value)
    atomic_write_json(
        path / "progress.json",
        {
            "schema_version": "depth_progress.v1",
            "state": "queued",
            "completed_frames": 0,
        },
    )
    return value


def _select_frames(data, submission):
    geometry = set(submission.geometry_hashes)
    sensors = set(submission.sensors)
    if len(geometry) != len(submission.geometry_hashes) or not geometry <= {
        o["canonical_sha256"] for o in data["objects"]
    }:
        raise ValueError("Selected frozen geometry is unknown or duplicated")
    if len(sensors) != len(submission.sensors) or not sensors <= {
        s["sensor_name"] for s in data["scenes"]
    }:
        raise ValueError("Selected sensor is unknown or duplicated")
    projections = [scene for scene in data["scenes"] if scene["sensor_name"] in sensors]
    require_pinhole_projections(projections)
    frames = [
        f
        for f in data["frames"]
        if f["sensor_name"] in sensors
        and any(g["canonical_sha256"] in geometry for g in f["gt"])
    ]
    if not frames:
        raise ValueError("No GT frames cover the selected geometry and sensors")
    return frames, projections


def create_evaluation(run_root, value):
    submission = EvaluationSubmission.model_validate(value)
    root = Path(run_root).resolve()
    if Path(submission.run_root).resolve() != root:
        raise ValueError("Depth request run identity differs")
    data = inventory(root)
    frames, projections = _select_frames(data, submission)
    paths = set(data["paths"]) | {f[k] for f in frames for k in ("rgb", "depth")}
    # Stat identities bind submission cheaply; workers hash all content before
    # use, and recheck both identities and hashes before atomic publication.
    sources = [identity(root, p) for p in sorted(paths)]
    return _store_request(
        root,
        "evaluations",
        {
            "schema_version": "depth_evaluation_request.v1",
            "id": f"depth-eval-{uuid.uuid4().hex[:12]}",
            "run_root": str(root),
            "created_at": now(),
            "submission": submission.model_dump(),
            "source_identity_sha256": digest(sources),
            "sources": sources,
            "frame_ids": [[f["scene_id"], f["im_id"]] for f in frames],
            "camera_projections": projections,
            "quality_evidence": data["quality_evidence"],
            "renderer_script_sha256": _sha256_file(
                APP_ROOT / "scripts/blenderproc_depth_reference.py"
            ),
            "implementation_sha256": {
                p: _sha256_file(APP_ROOT / p) for p in IMPLEMENTATION_FILES
            },
            "inventory_sha256": digest(data),
        },
    )


def _manifest_files(complete):
    return {
        p.relative_to(complete).as_posix(): _sha256_file(p)
        for p in sorted(complete.rglob("*"))
        if p.is_file() and p.name != "manifest.json"
    }


@dataclass(frozen=True)
class DepthPublication:
    request: dict
    directory: Path
    files: dict[str, str]
    manifest_sha256: str

    def artifact(self, relative: str) -> Path:
        if relative not in self.files:
            raise KeyError("Unknown depth published artifact")
        path = _safe_relative(self.directory, relative, label="Depth published file")
        _required_plain_file(path, root=self.directory, label="Depth published file")
        if _sha256_file(path) != self.files[relative]:
            raise ValueError("Depth published artifact hash changed")
        return path


def completed(
    root,
    kind,
    record_id,
    *,
    expected_manifest_sha256=_UNSPECIFIED_MANIFEST,
    verify_all=False,
):
    path = folder(root, kind, record_id)
    request = _request(root, kind, record_id)
    complete = path / "complete"
    _safe_relative(path, "complete", label="Depth completion")
    if not complete.is_dir():
        raise FileNotFoundError(
            "Depth output has not completed; partial downloads are unavailable"
        )
    manifest_path = _required_plain_file(
        complete / "manifest.json", root=path, label="Depth publication manifest"
    )
    contents = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(contents).hexdigest()
    if (
        expected_manifest_sha256 is not _UNSPECIFIED_MANIFEST
        and manifest_sha256 != expected_manifest_sha256
    ):
        raise ValueError("Depth source publication manifest changed")
    manifest = json.loads(contents)
    if (
        manifest.get("schema_version") != "depth_publication.v1"
        or manifest.get("id") != record_id
        or manifest.get("request_sha256") != _sha256_file(path / "request.json")
    ):
        raise ValueError("Depth completion does not bind the immutable request")
    files = manifest.get("files")
    if not isinstance(files, dict) or not {
        "report.json",
        "research.zip",
        "frames.sqlite3",
    } <= set(files):
        raise ValueError("Depth publication manifest is incomplete")
    publication = DepthPublication(request, complete, files, manifest_sha256)
    if verify_all:
        for relative in files:
            publication.artifact(relative)
    return publication


def artifact(root, kind, record_id, relative):
    return completed(root, kind, record_id).artifact(relative)


def record_status(root, kind, record_id):
    path = folder(root, kind, record_id)
    request = _request(root, kind, record_id)
    progress = read_json(path, "progress.json")
    # A killed worker can retain last progress, but a partial directory never
    # confers completion/download availability. Jobs remains authoritative.
    available = (path / "complete").is_dir()
    if available:
        completed(root, kind, record_id)
    return {
        "id": record_id,
        "created_at": request["created_at"],
        "state": "complete" if available else progress["state"],
        "report_available": available,
        "progress": progress,
        "submission": request.get("submission"),
        "inputs": request.get("inputs"),
    }


def history(root, kind, *, offset=0, limit=25, search=""):
    base = Path(root).resolve() / BASE / kind
    if _has_symlink_component(base, root=Path(root).resolve()):
        raise ValueError("Depth history uses a symbolic link")
    rows = []
    for path in base.glob(f"{KINDS[kind]}-*/request.json"):
        try:
            item = record_status(root, kind, path.parent.name)
        except (OSError, ValueError, KeyError) as exc:
            # One damaged record is reported as such. Raising here hid every
            # valid study of the run behind a single 400.
            item = {
                "id": path.parent.name,
                "created_at": "",
                "state": "invalid",
                "report_available": False,
                "progress": {"state": "invalid", "error": str(exc)},
                "submission": None,
                "inputs": None,
            }
        if search.lower() in json.dumps(item).lower():
            rows.append(item)
    rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
    return {
        "schema_version": "depth_history.v1",
        "records": rows[offset : offset + limit],
        "total": len(rows),
        "offset": offset,
        "limit": limit,
    }


def depth_setup(root):
    return {
        "schema_version": "depth_evaluation_setup.v1",
        "run_root": str(root),
        "dataset": setup(root),
        "renderer": renderer_status(),
        "defaults": MetricSettings().model_dump(),
        "assumptions": ASSUMPTIONS,
    }


def frames_page(
    root, kind, record_id, *, offset=0, limit=20, scene_id=None, input_id=None
):
    database = artifact(root, kind, record_id, "frames.sqlite3")
    clauses, parameters = [], []
    if scene_id is not None:
        clauses.append("json_extract(metadata, '$.scene_id') = ?")
        parameters.append(scene_id)
    if input_id is not None:
        clauses.append("json_extract(metadata, '$.input_id') = ?")
        parameters.append(input_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with open_readonly(database) as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM frames" + where, parameters
        ).fetchone()[0]
        rows = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT metadata FROM frames"
                + where
                + " ORDER BY ordinal LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            )
        ]
    return {
        "schema_version": "depth_frames.v1",
        "frames": rows,
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def _render(root, data, frames, work):
    if not renderer_status()["available"]:
        raise ValueError(renderer_status()["reason"])
    ids = {g["instance_uuid"]: g["obj_id"] for f in frames for g in f["gt"]}
    objects = {o["obj_id"]: o for o in data["objects"]}
    render_ids = {instance: i + 1 for i, instance in enumerate(sorted(ids))}
    plan = {
        "schema_version": "depth_render_plan.v1",
        "output": str(work / "reference"),
        "frames": frames,
        "instances": [
            {
                "instance_uuid": instance,
                "render_id": render_ids[instance],
                "path": str(root / objects[obj]["path"]),
            }
            for instance, obj in ids.items()
        ],
    }
    atomic_write_json(work / "render_plan.json", plan)
    subprocess.run(
        [
            "blenderproc",
            "run",
            "--temp-dir",
            str(work / ".blender-temp"),
            str(APP_ROOT / "scripts/blenderproc_depth_reference.py"),
            "--plan",
            str(work / "render_plan.json"),
        ],
        check=True,
        cwd=APP_ROOT,
    )
    return render_ids


def _validate_reference(path, frame, render_ids):
    with np.load(path, allow_pickle=False) as value:
        depth, normals, ids = (
            value["depth_mm"],
            value["normals_cv"],
            value["instance_ids"],
        )
    shape = frame["height"], frame["width"]
    if (
        depth.shape != shape
        or ids.shape != shape
        or normals.shape != (*shape, 3)
        or not np.issubdtype(ids.dtype, np.integer)
    ):
        raise ValueError("Depth renderer produced incompatible map dimensions/types")
    allowed = {0} | {render_ids[g["instance_uuid"]] for g in frame["gt"]}
    if (
        not set(np.unique(ids)) <= allowed
        or not np.isfinite(depth).all()
        or not np.isfinite(normals).all()
    ):
        raise ValueError("Depth renderer produced invalid maps/instance identities")
    mask = ids > 0
    if (
        np.any(depth[mask] <= 0)
        or np.any(depth[~mask] != 0)
        or not np.allclose(np.linalg.norm(normals[mask], axis=-1), 1, atol=0.02)
    ):
        raise ValueError(
            "Depth renderer produced invalid optical-axis depth or surface normals"
        )
    return depth, normals, ids


def _image(path, array):
    Image.fromarray(array).save(path)


def frame_media(
    destination, key, measured, reference, selected, rgb_path, depth_max, error_max
):
    """Identical scales for a record; missing magenta, outside dark grey."""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import colormaps

    outside, missing = (
        np.array([31, 41, 55], np.uint8),
        np.array([225, 55, 190], np.uint8),
    )
    valid = np.isfinite(measured) & (measured > 0)
    shutil.copyfile(rgb_path, destination / f"{key}-rgb.png")
    for name, pixels in (
        ("measured", measured),
        ("reference", reference),
        ("error", measured - reference),
    ):
        scale = (
            np.clip((pixels / error_max + 1) / 2, 0, 1)
            if name == "error"
            else np.clip(pixels / depth_max, 0, 1)
        )
        image = (
            colormaps["coolwarm" if name == "error" else "viridis"](
                np.nan_to_num(scale)
            )[..., :3]
            * 255
        ).astype(np.uint8)
        image[~selected] = outside
        if name != "reference":
            image[selected & ~valid] = missing
        _image(destination / f"{key}-{name}.png", image)
    image = np.full((*selected.shape, 3), outside, dtype=np.uint8)
    image[selected & valid] = [45, 180, 120]
    image[selected & ~valid] = missing
    _image(destination / f"{key}-missing.png", image)


def _tables_and_bundle(work, report, request, hashes):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    atomic_write_json(work / "report.json", report)
    atomic_write_json(work / "settings.json", request)
    atomic_write_json(work / "provenance.json", hashes)
    rows = report.get("aggregates", report.get("bins", []))
    with (work / "metrics.csv").open("w", newline="") as stream:
        fields = [
            "scope",
            "input_id",
            "finish_label",
            "sensor_name",
            "canonical_sha256",
            "instance_uuid",
            "region",
            "distance_bin",
            "angle_bin",
            "reference_pixels",
            "valid_pixels",
            "missing_pixels",
            "frame_count",
            "contributing_frame_count",
            "completeness",
            "bias_mm",
            "mae_mm",
            "rmse_mm",
            "median_absolute_error_mm",
            "p95_absolute_error_mm",
            "thresholds",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {**row, "thresholds": json.dumps(row.get("thresholds", []))}
            )
    plot_rows = [
        r for r in rows if r.get("region") == "interior" and r.get("scope") == "bin"
    ]
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), layout="constrained")
    # Keep each evaluation/run/sensor series explicit. No pooled finish ranking.
    series = {}
    for row in plot_rows:
        label = f"{row.get('input_id') or report['id']} · {row.get('finish_label') or report.get('finish_label', '')} · {row.get('sensor_name', '')} · {row.get('canonical_sha256', '')[:8]} · {row['angle_bin'] * report['settings']['angle_bin_deg']:g}° bin"
        series.setdefault(label, []).append(row)
    for label, items in series.items():
        items.sort(key=lambda r: r["distance_bin"])
        x = [r["distance_bin"] * report["settings"]["distance_bin_mm"] for r in items]
        axes[0].plot(
            x,
            [
                r.get("completeness") if r.get("completeness") is not None else np.nan
                for r in items
            ],
            "o",
            label=label,
        )
        axes[1].plot(
            x,
            [r.get("mae_mm") if r.get("mae_mm") is not None else np.nan for r in items],
            "o",
            label=label,
        )
    for ax, title in zip(
        axes, ("Completeness", "Mean absolute error (mm)"), strict=True
    ):
        ax.set_title(title)
        ax.set_xlabel("Reference distance bin start (mm)")
        ax.grid(alpha=0.2)
    if series:
        axes[0].legend(fontsize=5, loc="best")
    fig.savefig(work / "metrics.png", dpi=160)
    fig.savefig(work / "metrics.svg")
    plt.close(fig)
    # All content and figures are local. Embed JSON safely for a standalone,
    # offline report with filterable tables and retained frame inspection.
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False).replace(
        "<", "\\u003c"
    )
    (work / "report.html").write_text(
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>Depth evaluation</title>"
        "<style>body{font:14px system-ui;margin:2rem;background:#f8fafc;color:#172033}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd;padding:6px;text-align:left}pre{white-space:pre-wrap}img{max-width:100%}input{padding:8px} .maps{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}</style>"
        f"<h1>Depth {html.escape(report['kind'])}: {html.escape(report['id'])}</h1><p>"
        + " ".join(html.escape(a) for a in ASSUMPTIONS)
        + "</p>"
        "<p>Magenta: missing return. Dark grey: outside selected reference geometry. Error scales are symmetric in mm; depth scales are shared.</p>"
        "<p><a href='metrics.csv'>Metrics CSV</a> · <a href='report.json'>JSON</a> · <a href='settings.json'>Settings</a> · <a href='provenance.json'>Provenance</a></p>"
        "<img src='metrics.svg' alt='Depth completeness and absolute error'><h2>Evidence and settings</h2><pre id='evidence'></pre>"
        "<h2>Metrics</h2><label>Filter rows <input id='filter'></label><div style='overflow:auto'><table id='table'></table></div>"
        "<h2>Frames</h2><label>Frame <select id='frame'></select></label><div class='maps' id='maps'></div><pre id='frameinfo'></pre>"
        f"<script>const report={payload};document.getElementById('evidence').textContent=JSON.stringify({{settings:report.settings,quality:report.quality_evidence,scales:report.scales,coverage:report.coverage,inputs:report.inputs}},null,2);"
        "const rows=report.aggregates||report.bins||[];const fields=['input_id','finish_label','sensor_name','canonical_sha256','instance_uuid','scope','region','distance_bin','angle_bin','reference_pixels','valid_pixels','missing_pixels','frame_count','contributing_frame_count','completeness','bias_mm','mae_mm','rmse_mm','median_absolute_error_mm','p95_absolute_error_mm','thresholds'];"
        "function table(){const t=document.getElementById('table');t.replaceChildren();const h=t.insertRow();for(const f of fields){const c=document.createElement('th');c.textContent=f;h.append(c)}for(const row of rows.filter(r=>JSON.stringify(r).toLowerCase().includes(document.getElementById('filter').value.toLowerCase()))){const tr=t.insertRow();for(const f of fields){tr.insertCell().textContent=typeof row[f]==='object'?JSON.stringify(row[f]):row[f]??'unavailable'}}}document.getElementById('filter').oninput=table;table();"
        "fetch('frame_index.json').then(r=>r.json()).then(frames=>{const select=document.getElementById('frame');for(const f of frames){const o=document.createElement('option');o.value=f.key;o.textContent=f.key+' · '+f.sensor_name;select.append(o)}function show(){const key=select.value;const maps=document.getElementById('maps');maps.replaceChildren();for(const kind of ['rgb','measured','reference','error','missing']){const figure=document.createElement('figure');const img=document.createElement('img');img.src='frames/'+key+'-'+kind+'.png';img.alt=kind;figure.append(img);const cap=document.createElement('figcaption');cap.textContent=kind;figure.append(cap);maps.append(figure)}fetch('frames/'+key+'.json').then(r=>r.json()).then(f=>document.getElementById('frameinfo').textContent=JSON.stringify(f,null,2))}select.onchange=show;if(frames.length)show()});</script></html>"
    )
    # file:// reports cannot fetch local JSON in all browsers. Inline the index
    # and frame metadata so the extracted report also works without a server.
    with sqlite3.connect(work / "frames.sqlite3") as db:
        index = [
            json.loads(row[0])
            for row in db.execute("SELECT metadata FROM frames ORDER BY ordinal")
        ]
    atomic_write_json(work / "frame_index.json", index)
    text = (work / "report.html").read_text()
    frame_payload = json.dumps(index, ensure_ascii=False).replace("<", "\\u003c")
    text = text.replace(
        "fetch('frame_index.json').then(r=>r.json())",
        f"Promise.resolve({frame_payload})",
    )
    text = text.replace(
        "fetch('frames/'+key+'.json').then(r=>r.json())",
        "Promise.resolve(frames.find(f=>f.key===key))",
    )
    (work / "report.html").write_text(text)
    atomic_write_json(
        work / "bundle_manifest.json",
        {
            "schema_version": "depth_research_bundle.v1",
            "id": report["id"],
            "files": _manifest_files(work),
        },
    )
    with zipfile.ZipFile(
        work / "research.zip", "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as archive:
        for path in sorted(work.rglob("*")):
            if path.is_file() and path.name != "research.zip":
                archive.write(path, path.relative_to(work))


def _publish(path, work, request_hash, record_id, *, before_publish):
    atomic_write_json(
        work / "manifest.json",
        {
            "schema_version": "depth_publication.v1",
            "id": record_id,
            "request_sha256": request_hash,
            "files": _manifest_files(work),
        },
    )
    before_publish()
    os.rename(work, path / "complete")


def run_evaluation(request_path, *, render_reference=None):
    path = Path(request_path).absolute()
    value = json.loads(path.read_text())
    root, record_id = Path(value["run_root"]).resolve(), value["id"]
    target = folder(root, "evaluations", record_id)
    if value["run_root"] != str(root) or path != target / "request.json":
        raise ValueError("Depth worker request must live in its run-owned record")
    request = _request(root, "evaluations", record_id)
    submission = EvaluationSubmission.model_validate(request["submission"])
    request_hash = _sha256_file(path)
    with (target / ".started").open("x"):
        pass
    work = target / ".work"
    work.mkdir()
    media = work / "frames"
    media.mkdir()
    completed_frames = 0

    def progress(state, **extra):
        atomic_write_json(
            target / "progress.json",
            {
                "schema_version": "depth_progress.v1",
                "state": state,
                "completed_frames": completed_frames,
                "total_frames": len(request["frame_ids"]),
                **extra,
            },
        )

    def check():
        if (
            _sha256_file(path) != request_hash
            or _sha256_file(APP_ROOT / "scripts/blenderproc_depth_reference.py")
            != request["renderer_script_sha256"]
        ):
            raise ValueError("Depth request or renderer implementation changed")
        if any(
            _sha256_file(APP_ROOT / p) != h
            for p, h in request["implementation_sha256"].items()
        ):
            raise ValueError("Depth implementation changed after submission")

    try:
        progress("validating")
        check()
        if digest(request["sources"]) != request["source_identity_sha256"]:
            raise ValueError("Depth source identities differ from the frozen request")
        hashes = hash_sources(root, request["sources"])
        data = inventory(root)
        frames, projections = _select_frames(data, submission)
        if digest(data) != request["inventory_sha256"]:
            raise ValueError("Depth source inventory changed after submission")
        if projections != request.get("camera_projections"):
            raise ValueError("Depth camera projections differ from the frozen request")
        for obj in data["objects"]:
            if hashes[obj["path"]] != obj["canonical_sha256"]:
                raise ValueError(
                    "Canonical geometry content differs from its frozen hash"
                )
        if [[f["scene_id"], f["im_id"]] for f in frames] != request["frame_ids"]:
            raise ValueError("Depth frame selection changed")
        progress("rendering")
        render_ids = (render_reference or _render)(root, data, frames, work)
        depth_max = 1.0
        for frame in frames:
            key = f"{frame['scene_id']:06d}_{frame['im_id']:06d}"
            depth, _, _ = _validate_reference(
                work / "reference" / f"{key}.npz", frame, render_ids
            )
            depth_max = max(depth_max, float(np.max(depth)))
        scales = {
            "depth_max_mm": float(np.ceil(depth_max / 100) * 100),
            "error_max_mm": max(submission.settings.thresholds_mm),
            "missing_rgb": [225, 55, 190],
            "outside_rgb": [31, 41, 55],
        }
        aggregates = {}
        spool = work / ".quantiles"
        spool.mkdir()
        progress("measuring")
        with (
            sqlite3.connect(work / "frames.sqlite3") as db,
            (work / "frames.csv").open("w", newline="") as csv_stream,
        ):
            db.execute(
                "CREATE TABLE frames (ordinal INTEGER PRIMARY KEY, metadata TEXT NOT NULL)"
            )
            writer = csv.writer(csv_stream)
            writer.writerow(
                [
                    "scene_id",
                    "im_id",
                    "sensor_name",
                    "instance_uuid",
                    "canonical_sha256",
                    "region",
                    "metrics_json",
                ]
            )
            for frame in frames:
                verify_hashes(
                    root, {frame[k]: hashes[frame[k]] for k in ("rgb", "depth")}
                )
                key = f"{frame['scene_id']:06d}_{frame['im_id']:06d}"
                reference, normals, ids = _validate_reference(
                    work / "reference" / f"{key}.npz", frame, render_ids
                )
                source = root / frame["depth"]
                depth_missing = hashes[frame["depth"]] is None
                measured = (
                    np.zeros(reference.shape, np.uint16)
                    if depth_missing
                    else cv2.imread(str(source), cv2.IMREAD_UNCHANGED)
                )
                if (
                    measured is None
                    or measured.ndim != 2
                    or measured.shape != reference.shape
                    or measured.dtype not in (np.uint16, np.uint8)
                ):
                    raise ValueError("Measured depth map is corrupt or incompatible")
                measured = measured.astype(np.float64) * frame["camera"]["depth_scale"]
                distance, angle = reference_bins(
                    reference,
                    normals,
                    np.reshape(frame["camera"]["cam_K"], (3, 3)),
                    submission.settings,
                )
                instance_rows, selected_mask = [], np.zeros(ids.shape, bool)
                # Accumulate each run/sensor/geometry/bin once per frame even
                # when several instances of the same geometry are visible.
                frame_groups = {}
                for gt in frame["gt"]:
                    geometry = gt["canonical_sha256"]
                    if geometry not in submission.geometry_hashes:
                        continue
                    instance = gt["instance_uuid"]
                    masks = regions(ids, render_ids[instance])
                    selected_mask |= masks["full"]
                    results = {}
                    for region, mask in masks.items():
                        metrics, errors = measure(
                            measured, reference, mask, submission.settings
                        )
                        results[region] = metrics
                        writer.writerow(
                            [
                                frame["scene_id"],
                                frame["im_id"],
                                frame["sensor_name"],
                                instance,
                                geometry,
                                region,
                                json.dumps(metrics),
                            ]
                        )
                        groups = [
                            ("run", "", "", "", region, -1, -1),
                            ("sensor", frame["sensor_name"], "", "", region, -1, -1),
                            (
                                "sensor_geometry",
                                frame["sensor_name"],
                                geometry,
                                "",
                                region,
                                -1,
                                -1,
                            ),
                            (
                                "instance",
                                frame["sensor_name"],
                                geometry,
                                instance,
                                region,
                                -1,
                                -1,
                            ),
                        ]
                        for group in groups:
                            frame_groups.setdefault(group, []).append(
                                (int(mask.sum()), errors)
                            )
                        for d, a in np.unique(
                            np.stack((distance[mask], angle[mask]), axis=-1), axis=0
                        ):
                            bin_mask = mask & (distance == d) & (angle == a)
                            _, bin_errors = measure(
                                measured, reference, bin_mask, submission.settings
                            )
                            group = (
                                "bin",
                                frame["sensor_name"],
                                geometry,
                                "",
                                region,
                                int(d),
                                int(a),
                            )
                            frame_groups.setdefault(group, []).append(
                                (int(bin_mask.sum()), bin_errors)
                            )
                    instance_rows.append(
                        {
                            "instance_uuid": instance,
                            "obj_id": gt["obj_id"],
                            "canonical_sha256": geometry,
                            "regions": results,
                        }
                    )
                for group, pieces in frame_groups.items():
                    if group not in aggregates:
                        aggregates[group] = Aggregate(
                            spool / f"{len(aggregates)}.f64", submission.settings
                        )
                    aggregates[group].add(
                        sum(n for n, _ in pieces),
                        np.concatenate([e for _, e in pieces]),
                    )
                np.savez_compressed(
                    media / f"{key}.npz",
                    measured_mm=measured,
                    depth_mm=reference,
                    normals_cv=normals,
                    instance_ids=ids,
                    selected=selected_mask,
                )
                frame_media(
                    media,
                    key,
                    measured,
                    reference,
                    selected_mask,
                    root / frame["rgb"],
                    scales["depth_max_mm"],
                    scales["error_max_mm"],
                )
                metadata = {
                    "key": key,
                    "scene_id": frame["scene_id"],
                    "im_id": frame["im_id"],
                    "sensor_name": frame["sensor_name"],
                    "width": frame["width"],
                    "height": frame["height"],
                    "instances": instance_rows,
                    "reference_camera": frame["camera"],
                    "reference_instances": [
                        {**g, "render_id": render_ids[g["instance_uuid"]]}
                        for g in frame["gt"]
                    ],
                    "scales": scales,
                    "missing_depth_map": depth_missing,
                }
                atomic_write_json(media / f"{key}.json", metadata)
                db.execute(
                    "INSERT INTO frames VALUES (?, ?)",
                    (completed_frames, json.dumps(metadata)),
                )
                completed_frames += 1
                progress("measuring")
        rows = []
        for group, accumulator in aggregates.items():
            scope, sensor, geometry, instance, region, distance_bin, angle_bin = group
            rows.append(
                {
                    "scope": scope,
                    "sensor_name": sensor,
                    "canonical_sha256": geometry,
                    "instance_uuid": instance,
                    "region": region,
                    "distance_bin": distance_bin,
                    "angle_bin": angle_bin,
                    **accumulator.result(),
                }
            )
        shutil.rmtree(spool)
        shutil.rmtree(work / "reference" / ".render", ignore_errors=True)
        report = {
            "schema_version": "depth_evaluation_report.v1",
            "kind": "evaluation",
            "id": record_id,
            "metric_revision": REVISION,
            "run_root": str(root),
            "finish_label": submission.finish_label,
            "settings": submission.settings.model_dump(),
            "geometry_hashes": submission.geometry_hashes,
            "sensors": submission.sensors,
            "camera_projections": projections,
            "frame_count": completed_frames,
            "aggregates": rows,
            "scales": scales,
            "quality_evidence": request["quality_evidence"],
            "assumptions": ASSUMPTIONS,
            "renderer": read_json(work, "reference/renderer.json"),
            "source_sha256": hashes,
            "source_identity_sha256": request["source_identity_sha256"],
            "implementation_sha256": request["implementation_sha256"],
            "created_at": now(),
        }
        report = EvaluationReport.model_validate(report).model_dump()
        progress("reporting")
        # Renderer working files are not part of the study. Every reference
        # map is already in frames/<key>.npz, and the plan records absolute
        # host paths; publishing them doubled the study on disk and again
        # inside the research ZIP.
        shutil.rmtree(work / "reference")
        (work / "render_plan.json").unlink(missing_ok=True)
        shutil.rmtree(work / ".blender-temp", ignore_errors=True)
        _tables_and_bundle(work, report, request, hashes)

        def final_check():
            check()
            verify_identities(root, request["sources"])
            verify_hashes(root, hashes)

        _publish(target, work, request_hash, record_id, before_publish=final_check)
        progress("complete")
        return report
    except BaseException as exc:
        write_terminal_progress(progress, exc, DepthCanceled)
        raise


def _comparison_input(root, publication):
    report = json.loads(publication.artifact("report.json").read_text())
    request = publication.request
    if (
        report.get("schema_version") != "depth_evaluation_report.v1"
        or report.get("id") != request["id"]
        or report.get("run_root") != request["run_root"]
        or report.get("sensors") != request["submission"]["sensors"]
    ):
        raise ValueError("Depth evaluation report does not match its frozen request")
    scene_ids = {frame_id[0] for frame_id in request["frame_ids"]}
    projections = request.get("camera_projections")
    if projections is None:
        projections = retained_projections(
            root, report["source_sha256"][BOP_MANIFEST], scene_ids
        )
    elif projections != report.get("camera_projections"):
        raise ValueError(
            "Depth report projection evidence differs from its frozen request"
        )
    if (
        len(projections) != len(scene_ids)
        or {scene["scene_id"] for scene in projections} != scene_ids
        or {scene["sensor_name"] for scene in projections} != set(report["sensors"])
    ):
        raise ValueError(
            "Depth projection evidence does not cover the selected frames and sensors"
        )
    require_pinhole_projections(projections)
    return report, projections


def create_comparison(run_root, value):
    submission = ComparisonSubmission.model_validate(value)
    root = Path(run_root).resolve()
    if Path(submission.run_root).resolve() != root:
        raise ValueError("Depth comparison owner differs from its run")
    identities, reports, inputs = set(), [], []
    for item in submission.evaluations:
        source_root = Path(item.run_root).resolve()
        key = str(source_root), item.evaluation_id
        if key in identities:
            raise ValueError("Depth comparison evaluation inputs are duplicated")
        identities.add(key)
        publication = completed(source_root, "evaluations", item.evaluation_id)
        report, projections = _comparison_input(source_root, publication)
        reports.append(report)
        inputs.append(
            {
                **item.model_dump(),
                "run_root": str(source_root),
                "manifest_sha256": publication.manifest_sha256,
                "camera_projections": projections,
            }
        )
    if str(root) not in {key[0] for key in identities}:
        raise ValueError("Active run must be one of the comparison inputs")
    if any(r["settings"] != reports[0]["settings"] for r in reports):
        raise ValueError("Depth comparison metric settings are incompatible")
    if any(r["metric_revision"] != reports[0]["metric_revision"] for r in reports):
        raise ValueError("Depth comparison metric revisions are incompatible")
    common_geometry = sorted(
        set.intersection(*(set(r["geometry_hashes"]) for r in reports))
    )
    if not common_geometry:
        raise ValueError(
            "Depth comparisons require a shared frozen canonical geometry hash"
        )
    # One finish per run within a study, including comparisons of sensors.
    finishes = {}
    for item, report in zip(inputs, reports, strict=True):
        run = item["run_root"]
        if run in finishes and finishes[run] != report["finish_label"]:
            raise ValueError(
                "Comparison inputs assign conflicting finish labels to one run"
            )
        finishes[run] = report["finish_label"]
    return _store_request(
        root,
        "comparisons",
        {
            "schema_version": "depth_comparison_request.v1",
            "id": f"depth-compare-{uuid.uuid4().hex[:12]}",
            "run_root": str(root),
            "created_at": now(),
            "inputs": inputs,
            "common_geometry_hashes": common_geometry,
            "settings": reports[0]["settings"],
            "metric_revision": reports[0]["metric_revision"],
            "implementation_sha256": {
                p: _sha256_file(APP_ROOT / p) for p in IMPLEMENTATION_FILES
            },
            "submission": submission.model_dump(),
        },
    )


def run_comparison(request_path):
    path = Path(request_path).absolute()
    raw = json.loads(path.read_text())
    root, record_id = Path(raw["run_root"]).resolve(), raw["id"]
    target = folder(root, "comparisons", record_id)
    if raw["run_root"] != str(root) or path != target / "request.json":
        raise ValueError("Depth comparison request must live in its run-owned record")
    request = _request(root, "comparisons", record_id)
    request_hash = _sha256_file(path)
    with (target / ".started").open("x"):
        pass
    work = target / ".work"
    work.mkdir()
    (work / "frames").mkdir()

    def progress(state, **extra):
        atomic_write_json(
            target / "progress.json",
            {"schema_version": "depth_progress.v1", "state": state, **extra},
        )

    def check():
        if _sha256_file(path) != request_hash:
            raise ValueError("Depth comparison request changed")
        if any(
            _sha256_file(APP_ROOT / p) != h
            for p, h in request["implementation_sha256"].items()
        ):
            raise ValueError("Depth comparison implementation changed")

        publications, reports = [], []
        for item in request["inputs"]:
            source_root = Path(item["run_root"])
            publication = completed(
                source_root,
                "evaluations",
                item["evaluation_id"],
                expected_manifest_sha256=item["manifest_sha256"],
                verify_all=True,
            )
            report, projections = _comparison_input(source_root, publication)
            if projections != item["camera_projections"]:
                raise ValueError("Depth comparison input projection changed")
            publications.append(publication)
            reports.append(report)
        return publications, reports

    try:
        progress("validating")
        publications, reports = check()
        geometry = request["common_geometry_hashes"]
        # Each evaluation/sensor is a separate series. Shared support is the
        # intersection across those series within a geometry/region.
        series = []
        for i, report in enumerate(reports):
            for sensor in report["sensors"]:
                rows = [
                    r
                    for r in report["aggregates"]
                    if r["scope"] == "bin"
                    and r["sensor_name"] == sensor
                    and r["canonical_sha256"] in geometry
                ]
                series.append(
                    {
                        "input_id": str(i),
                        "evaluation_id": report["id"],
                        "run_root": request["inputs"][i]["run_root"],
                        "finish_label": report["finish_label"],
                        "sensor_name": sensor,
                        "rows": rows,
                    }
                )
        common, coverage, bins = {}, [], []
        for g in geometry:
            for region in ("full", "interior", "boundary"):
                supports = [
                    {
                        (r["distance_bin"], r["angle_bin"])
                        for r in s["rows"]
                        if r["canonical_sha256"] == g and r["region"] == region
                    }
                    for s in series
                ]
                support = set.intersection(*supports) if supports else set()
                common[g, region] = support
        for s in series:
            selected_rows = s.pop("rows")
            for g in geometry:
                for region in ("full", "interior", "boundary"):
                    rows = [
                        r
                        for r in selected_rows
                        if r["canonical_sha256"] == g and r["region"] == region
                    ]
                    matched = [
                        r
                        for r in rows
                        if (r["distance_bin"], r["angle_bin"]) in common[g, region]
                    ]
                    coverage.append(
                        {
                            **s,
                            "canonical_sha256": g,
                            "region": region,
                            "common_bin_count": len(matched),
                            "common_reference_pixels": sum(
                                r["reference_pixels"] for r in matched
                            ),
                            "unmatched_reference_pixels": sum(
                                r["reference_pixels"] for r in rows if r not in matched
                            ),
                            "unmatched_bins": [r for r in rows if r not in matched],
                            "status": "available" if matched else "unavailable",
                            "reason": None
                            if matched
                            else "No common distance/incidence bins across the selected run/sensor series.",
                        }
                    )
                    bins.extend({**r, **s} for r in matched)
        scales = {
            "depth_max_mm": max(r["scales"]["depth_max_mm"] for r in reports),
            "error_max_mm": max(request["settings"]["thresholds_mm"]),
        }
        progress("reporting")
        ordinal = 0
        total_frames = sum(r["frame_count"] for r in reports)
        with sqlite3.connect(work / "frames.sqlite3") as db:
            db.execute(
                "CREATE TABLE frames (ordinal INTEGER PRIMARY KEY, metadata TEXT NOT NULL)"
            )
            for i, publication in enumerate(publications):
                with open_readonly(publication.artifact("frames.sqlite3")) as src:
                    for row in src.execute(
                        "SELECT metadata FROM frames ORDER BY ordinal"
                    ):
                        frame = json.loads(row[0])
                        key, old_key = f"{i}_{frame['key']}", frame["key"]
                        numeric = publication.artifact(f"frames/{old_key}.npz")
                        rgb = publication.artifact(f"frames/{old_key}-rgb.png")
                        with np.load(numeric, allow_pickle=False) as arrays:
                            frame_media(
                                work / "frames",
                                key,
                                arrays["measured_mm"],
                                arrays["depth_mm"],
                                arrays["selected"],
                                rgb,
                                scales["depth_max_mm"],
                                scales["error_max_mm"],
                            )
                        shutil.copyfile(numeric, work / "frames" / f"{key}.npz")
                        metadata = {
                            **frame,
                            "key": key,
                            "input_id": str(i),
                            "finish_label": reports[i]["finish_label"],
                            "scales": scales,
                        }
                        atomic_write_json(work / "frames" / f"{key}.json", metadata)
                        db.execute(
                            "INSERT INTO frames VALUES (?, ?)",
                            (ordinal, json.dumps(metadata)),
                        )
                        ordinal += 1
                        progress(
                            "reporting",
                            completed_frames=ordinal,
                            total_frames=total_frames,
                        )
        report = {
            "schema_version": "depth_comparison_report.v1",
            "kind": "comparison",
            "id": record_id,
            "metric_revision": request["metric_revision"],
            "implementation_sha256": request["implementation_sha256"],
            "inputs": request["inputs"],
            "geometry_hashes": geometry,
            "settings": request["settings"],
            "series": series,
            "bins": bins,
            "coverage": coverage,
            "scales": scales,
            "frame_count": ordinal,
            "assumptions": ASSUMPTIONS,
            "quality_evidence": [
                {"input_id": str(i), "evidence": r["quality_evidence"]}
                for i, r in enumerate(reports)
            ],
            "created_at": now(),
        }
        report = ComparisonReport.model_validate(report).model_dump()
        _tables_and_bundle(work, report, request, request["inputs"])

        _publish(target, work, request_hash, record_id, before_publish=check)
        progress("complete", completed_frames=ordinal)
        return report
    except BaseException as exc:
        write_terminal_progress(progress, exc, DepthCanceled)
        raise
