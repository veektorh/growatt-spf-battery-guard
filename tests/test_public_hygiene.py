import unittest

from scripts.check_public_hygiene import find_violations_in_text


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


if __name__ == "__main__":
    unittest.main()
