"""Keep test-created apps away from the lab's persistent working data."""

from pathlib import Path

import pytest

from posetestbot.web import security


@pytest.fixture(autouse=True)
def isolated_working_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sandbox = tmp_path / "isolated-working"
    working = sandbox / "working_data"
    working.mkdir(parents=True)
    monkeypatch.setenv("POSETESTBOT_WORKING_DATA_ROOT", str(working))
    for name in (
        "POSETESTBOT_WEB_RUN_ROOTS", "POSETESTBOT_WEB_DEFAULT_RUN_ROOT", "POSETESTBOT_WEB_INPUT_ROOTS",
        "POSETESTBOT_CLUSTER_ENABLED", "POSETESTBOT_CLUSTER_ENV_FILE", "POSETESTBOT_CLUSTER_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(security, "DEFAULT_RUN_ROOT", working)
    monkeypatch.setattr(security, "DEFAULT_RUN_ROOTS", (working, sandbox / "ssd"))
