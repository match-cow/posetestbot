from __future__ import annotations

import pytest

from posetestbot.io._report_checks import make_check, overall_status


def test_make_check_copies_details_mapping() -> None:
    details = {"count": 3}

    check = make_check("sensor", "warning", "Check the sensor.", details=details)
    details["late_change"] = True

    assert check == {
        "name": "sensor",
        "status": "warning",
        "message": "Check the sensor.",
        "details": {"count": 3},
    }


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([], "ok"),
        ([{"status": "ok"}], "ok"),
        ([{"status": "warning"}, {"status": "ok"}], "warning"),
        ([{"status": "warning"}, {"status": "error"}], "error"),
    ],
)
def test_overall_status_uses_error_warning_ok_precedence(
    checks: list[dict[str, str]],
    expected: str,
) -> None:
    assert overall_status(checks) == expected
