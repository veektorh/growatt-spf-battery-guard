import tempfile
import unittest
from pathlib import Path

from scripts.check_public_hygiene import SENSITIVE_ENV_KEYS, find_violations_in_text, validate_sample_env


class PublicHygieneCheckTests(unittest.TestCase):
    def test_allows_public_placeholders(self):
        text = "\n".join(
            [
                "GROWATT_USERNAME=your_shinephone_username",
                "GROWATT_PASSWORD=...",
                "GROWATT_PLANT_ID=",
                "WEATHER_LAT=your_latitude",
                "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...",
            ]
        )

        self.assertEqual(find_violations_in_text("example", text), [])

    def test_flags_likely_real_values(self):
        text = "\n".join(
            [
                "GROWATT_USERNAME=private_value",
                "WEATHER_LAT=latitude_value",
                "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/" + "1" + "23456" + "/token",
            ]
        )

        violations = find_violations_in_text("bad.env", text)

        self.assertEqual(len(violations), 4)
        messages = "\n".join(v.message for v in violations)
        self.assertIn("GROWATT_USERNAME", messages)
        self.assertIn("WEATHER_LAT", messages)
        self.assertIn("Discord webhook", messages)
        self.assertIn("DISCORD_WEBHOOK_URL", messages)

    def test_sample_env_requires_all_sensitive_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sample_path = Path(tmpdir) / ".env.example"
            sample_path.write_text("GROWATT_USERNAME=your_shinephone_username\n", encoding="utf-8")

            violations = validate_sample_env(sample_path)

        messages = "\n".join(v.message for v in violations)
        self.assertIn("missing public-safe sample for GROWATT_PASSWORD", messages)
        missing_count = sum(1 for v in violations if v.message.startswith("missing public-safe sample"))
        self.assertEqual(missing_count, len(SENSITIVE_ENV_KEYS) - 1)

    def test_sample_env_allows_public_placeholders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sample_path = Path(tmpdir) / ".env.example"
            sample_path.write_text(
                "\n".join(f"{key}=" for key in sorted(SENSITIVE_ENV_KEYS)) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(validate_sample_env(sample_path), [])

    def test_sample_env_flags_realistic_sensitive_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sample_path = Path(tmpdir) / ".env.example"
            sample_path.write_text(
                "\n".join(f"{key}=" for key in sorted(SENSITIVE_ENV_KEYS - {"PVOUTPUT_API_KEY"}))
                + "\nPVOUTPUT_API_KEY=abc123\n",
                encoding="utf-8",
            )

            violations = validate_sample_env(sample_path)

        self.assertTrue(any("PVOUTPUT_API_KEY" in v.message for v in violations))


if __name__ == "__main__":
    unittest.main()
