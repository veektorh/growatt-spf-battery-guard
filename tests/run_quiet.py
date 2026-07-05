from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.path.insert(0, str(ROOT))
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="growatt_guard_tests_") as state_dir:
        os.environ["GROWATT_GUARD_STATE_DIR"] = state_dir
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(verbosity=0, buffer=True).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
