import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from growatt_guard.audit import summarize_today_log_counts


class LogSummaryTests(unittest.TestCase):
    def test_transient_retries_are_not_counted_as_failures(self):
        today = dt.datetime.now().strftime("%Y-%m-%d")
        log_text = "\n".join(
            [
                f"{today} 20:28:38,145 WARNING Growatt API call failed (attempt 1/3): expired. Retrying in 5s.",
                f"{today} 20:28:44,988 INFO Current status: soc=44%",
                f"{today} 20:29:00,000 ERROR Attempting a reconnect in 0.12s",
                f"{today} 20:29:03,000 ERROR Attempting a reconnect in 2.81s",
                f"{today} 20:29:06,000 INFO Shard ID None has successfully RESUMED session.",
                f"{today} 21:00:00,000 ERROR PVOutput upload failed: 500 server error",
                f"{today} 21:01:00,000 ERROR Unhandled error",
            ]
        )

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.LOG_FILE", Path(tmpdir) / "log.txt"):
            Path(tmpdir, "log.txt").write_text(log_text, encoding="utf-8")

            counts = summarize_today_log_counts()

        self.assertEqual(counts["failure"], 2)

    def test_unresolved_discord_reconnects_count_as_one_failure(self):
        today = dt.datetime.now().strftime("%Y-%m-%d")
        log_text = "\n".join(
            [
                f"{today} 20:29:00,000 ERROR Attempting a reconnect in 0.12s",
                f"{today} 20:29:03,000 ERROR Attempting a reconnect in 2.81s",
                f"{today} 20:29:06,000 ERROR Attempting a reconnect in 24.64s",
            ]
        )

        with TemporaryDirectory() as tmpdir, patch("growatt_guard.audit.LOG_FILE", Path(tmpdir) / "log.txt"):
            Path(tmpdir, "log.txt").write_text(log_text, encoding="utf-8")

            counts = summarize_today_log_counts()

        self.assertEqual(counts["failure"], 1)


if __name__ == "__main__":
    unittest.main()
