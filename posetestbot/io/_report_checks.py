"""Internal helpers for status-check records shared by generated reports."""

from __future__ import annotations

from typing import Any, Mapping


def make_check(
    name: str,
    status: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the common status-check record with a copied details mapping."""

    return {
        "name": name,
        "status": status,
        "message": message,
        "details": dict(details or {}),
    }


def overall_status(checks: list[Mapping[str, Any]]) -> str:
    """Fold report checks using the shared error, warning, then ok precedence."""

    statuses = {str(check.get("status")) for check in checks}
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    return "ok"
