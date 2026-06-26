# Public-Safe Growatt Fixtures

These fixtures are synthetic/redacted SPF payloads used for parser tests. They
must not contain real plant IDs, device serials, datalogger serials, usernames,
coordinates, IP addresses, webhook URLs, or generated probe dumps.

Use these for dashboard, PVOutput, status-summary, and Discord dashboard parsing
coverage when Growatt returns duplicate or inconsistent fields.

To prepare a new fixture from a raw JSON probe, run:

```bash
python growatt_power_guard.py redact-probe logs/raw_probe.json --output tests/fixtures/new_fixture.json
```

Then manually review the output before committing it. Keep only the fields needed
for the parser case under test.
