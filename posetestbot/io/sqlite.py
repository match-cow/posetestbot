"""SQLite access to retained evidence."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def open_readonly(path: Path) -> closing[sqlite3.Connection]:
    return closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True))
