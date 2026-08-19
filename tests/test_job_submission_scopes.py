from __future__ import annotations

import ast
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_DIRECT_SUBMISSIONS = {
    "posetestbot/web/route_support.py": {
        "_submit_recipe": "run",
        "robot_commands": "<forwarded>",
    },
    "posetestbot/web/routes/bop_annotations.py": {
        "queue_bop_annotations": "run",
    },
    "posetestbot/web/routes/bop_evaluation.py": {
        "queue_bop_evaluation": "run",
    },
    "posetestbot/web/routes/calibration.py": {
        "calibration_attempt_create_endpoint": "run",
        "calibration_attempt_promote_endpoint": "run",
    },
    "posetestbot/web/routes/calibration_targets.py": {
        "calibration_target_generate": "library",
        "calibration_target_select": "run",
    },
    "posetestbot/web/routes/monitoring.py": {
        "_submit_monitor_service": "global",
    },
    "posetestbot/web/routes/pose_templates.py": {
        "_submit": "<forwarded>",
        "preview": "global",
        "library_delete": "library",
    },
    "posetestbot/web/routes/run_folders.py": {
        "refresh_run_folder_inventory": "global",
        "move_run_folder": "run",
        "delete_run_folder": "run",
    },
    "posetestbot/web/routes/sensors.py": {
        "_preview_submission": "global",
        "post_sensor_snapshots": "global",
    },
    "posetestbot/web/routes/workpieces.py": {
        "workpiece_catalog_upload": "library",
        "workpiece_catalog_unit_correction": "library",
    },
}

EXPECTED_POSE_TEMPLATE_HELPER_SCOPES = {
    "analyze_workpiece_orientations": "library",
    "generate": "library",
    "library_action": "library",
    "run_selection_update": "run",
}


class _CallCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.function_names: list[str] = []
        self.direct_submissions: dict[str, ast.Call] = {}
        self.helper_submissions: dict[str, ast.Call] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_names.append(node.name)
        self.generic_visit(node)
        self.function_names.pop()

    def visit_Call(self, node: ast.Call) -> None:
        function_name = self.function_names[-1] if self.function_names else "<module>"
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "submit"
            and (
                (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "job_runner"
                )
                or (
                    isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Name)
                    and node.func.value.func.id == "get_job_runner"
                    and not node.func.value.args
                    and not node.func.value.keywords
                )
            )
        ):
            assert function_name not in self.direct_submissions
            self.direct_submissions[function_name] = node
        elif isinstance(node.func, ast.Name) and node.func.id == "_submit":
            assert function_name not in self.helper_submissions
            self.helper_submissions[function_name] = node
        self.generic_visit(node)


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next(
        (keyword.value for keyword in call.keywords if keyword.arg == name),
        None,
    )


def _declared_scope(call: ast.Call) -> str:
    scope = _keyword(call, "scope_kind")
    assert scope is not None, "Every LocalJobRunner submission must choose scope_kind"
    if isinstance(scope, ast.Constant) and isinstance(scope.value, str):
        return scope.value
    if isinstance(scope, ast.Name) and scope.id == "scope_kind":
        return "<forwarded>"
    return ast.unparse(scope)


def test_every_direct_job_submission_declares_authoritative_scope() -> None:
    actual: dict[str, dict[str, str]] = {}
    for relative_path in EXPECTED_DIRECT_SUBMISSIONS:
        tree = ast.parse((REPOSITORY_ROOT / relative_path).read_text())
        collector = _CallCollector()
        collector.visit(tree)
        actual[relative_path] = {
            function_name: _declared_scope(call)
            for function_name, call in collector.direct_submissions.items()
        }
        for function_name, call in collector.direct_submissions.items():
            scope = actual[relative_path][function_name]
            run_root = _keyword(call, "run_root")
            if scope == "run":
                assert run_root is not None, (
                    f"{relative_path}:{function_name} must declare run_root"
                )
            elif scope in {"library", "global"}:
                assert run_root is None, (
                    f"{relative_path}:{function_name} must not declare run_root"
                )

    assert actual == EXPECTED_DIRECT_SUBMISSIONS


def test_pose_template_helper_callers_classify_each_authoring_endpoint() -> None:
    relative_path = "posetestbot/web/routes/pose_templates.py"
    tree = ast.parse((REPOSITORY_ROOT / relative_path).read_text())
    collector = _CallCollector()
    collector.visit(tree)

    actual = {}
    for function_name, call in collector.helper_submissions.items():
        scope = _keyword(call, "scope_kind")
        actual[function_name] = (
            scope.value
            if isinstance(scope, ast.Constant) and isinstance(scope.value, str)
            else "library"
        )
        if actual[function_name] == "run":
            assert _keyword(call, "run_root") is not None
        else:
            assert _keyword(call, "run_root") is None

    assert actual == EXPECTED_POSE_TEMPLATE_HELPER_SCOPES
