import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from growatt_guard.audit import summarize_today_log_counts


class LogSummaryTests(unittest.TestCase):
    def test_transient_growatt_retry_warning_is_not_counted_as_failure(self):
        today = dt.datetime.now().strftime("%Y-%m-%d")
        log_text = "\n".join(
            [
                f"{today} 20:28:38,145 WARNING Growatt API call failed (attempt 1/3): expired. Retrying in 5s.",
                f"{today} 20:28:44,988 INFO Current status: soc=44%",
                f"{today} 21:00:00,000 ERROR PVOutput upload failed: 500 server error",
                f"{today} 21:01:00,000 ERROR Unhandled error",
            ]
        )

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.LOG_FILE", Path(tmpdir) / "log.txt"):
            Path(tmpdir, "log.txt").write_text(log_text, encoding="utf-8")

            counts = summarize_today_log_counts()

        self.assertEqual(counts["failure"], 2)


if __name__ == "__main__":
    unittest.main()
