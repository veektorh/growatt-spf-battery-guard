from __future__ import annotations

import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _configured_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else default


# Immutable, release-owned files such as schedule.json live under APP_HOME.
# Mutable operational files stay under DATA_HOME so atomic release switches do
# not replace credentials, locks, state, logs, backups, or dashboard output.
APP_HOME = _configured_path("GROWATT_GUARD_HOME", PACKAGE_ROOT)
DATA_HOME = _configured_path("GROWATT_GUARD_DATA_DIR", APP_HOME)
