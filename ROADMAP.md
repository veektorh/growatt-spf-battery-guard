# Roadmap And Enhancement Ideas

This is a parking lot for improvements that would make the Growatt guard safer, easier to operate, and more useful over time. Items are grouped by likely value and effort, not strict commitment.

## Completed

- **Module split** — `modes.py`, `health.py`, `pause.py`; `growatt_power_guard.py` is a thin shim.
- **`run-scheduled --dry-plan`** — shows what would run, override status, pause state, DRY_RUN flag.
- **`schedule-preview`** — upcoming jobs, override status, skipped/replaced jobs.
- **`schedule-override` CLI** — `list`, `add-skip`, `add-skip-all`, `add-replace`, `remove` subcommands; validates before writing.
- **Named outage profiles** — `outage-profile apply skip-all/maintenance/health-only DATE...`; `health-only` replaces mode-changing jobs with health-check automatically.
- **`clear-stale-lock`** — removes a lock file older than the 45-minute threshold.
- **Dashboard operations sections** — Today's Schedule (with override status), Upcoming Overrides, skip-all banner.
- **Dashboard 7-day history chart** — inline canvas chart: preserve checks, utility switches, watchdog repairs per day.
- **Weather cache** — `state/weather_cache.json` with 15-minute TTL; avoids repeated Open-Meteo calls.
- **Season profiles** — `SEASON_PROFILES_ENABLED=true`; rainy season (Apr–Oct) uses config thresholds; dry season (Nov–Mar) lowers thresholds to 45/40/35%.
- **Tests split by module** — `test_schedule.py`, `test_dashboard.py`, `test_growatt_api.py`, `test_weather.py`, `test_pause.py`, `test_notifications.py`.
- **Update health gate** — `update_server.sh` runs compile/tests/validate-schedule before reinstalling cron; post-deploy smoke checks.
- **Monthly summary** — `monthly-summary` command (30-day audit window).
- **Weekly recommendations** — weekly summary includes threshold, watchdog, and failure tips from audit data.

---

## Highest Value Next

1. Make health-check more actionable
   - Include exact remediation hints for failed checks.
   - Include cron job count and next scheduled run summary.
   - Include whether `DRY_RUN` is enabled and whether any mode-changing job is currently paused.
   - Include dashboard service status when running on Linux with systemd.

2. Add stronger mode-write confirmation
   - After `set_mode`, wait briefly and re-read `outputConfig`.
   - Mark command as confirmed only when the status changes to the expected mode.
   - Send Discord warning if Growatt accepts the command but the mode does not verify.

3. Add dashboard JSON output
   - Generate `dashboard.json` with the same data used by `dashboard.html`.
   - Makes it easier to integrate with uptime monitors or other dashboards.

4. Add manual action copy-paste panel to dashboard
   - Static section showing ready-to-paste commands: pause, resume, health-check, dashboard refresh.
   - Avoid live write buttons until auth/CSRF is designed.

## Safety And Reliability

1. Add retry policy for transient Growatt reads
   - Retry login/status calls on network timeouts and 5xx responses.
   - Use short backoff to avoid hammering the cloud API.
   - Keep mode writes conservative: do not blindly retry writes many times.

2. Add command idempotency checks
   - Before switching to Utility, skip if already Utility.
   - Before switching to SBU, skip if already SBU, except when `--force` is explicitly provided.
   - Still write an audit row for skipped idempotent actions.

3. Add emergency mode guard
   - If SOC is below an emergency threshold, prevent automatic SBU return unless explicitly allowed.
   - Needs careful user preference because outage windows may still require SBU.

4. Add rate-limit guardrails
   - Track recent Growatt API calls in local state.
   - Warn if new scheduled jobs would create unusually frequent cloud reads.

## Scheduling Improvements

1. Add calendar export
   - Generate `.ics` calendar events for outage windows and automation jobs.
   - Useful for visually checking schedules before deployment.

2. Add schedule lint warnings
   - Warn if `return-sbu` is too close to an outage start.
   - Warn if preserve and return jobs overlap.
   - Warn if a mode-changing command runs too frequently.

3. Add dry-run cron installer mode
   - Show the exact crontab that would be installed.
   - Validate all job IDs before touching the user's crontab.

## Dashboard Enhancements

1. Add dashboard access hardening notes
   - Document recommended reverse proxy options.
   - Include security reminders for public exposure.

2. Add mobile-friendly compact mode
   - Smaller summary blocks for phone viewing.
   - Prioritize SOC, output mode, next job, pause state, and freshness.

## Notifications And Reporting

1. Add Discord embeds
   - Use structured fields for SOC, threshold, mode, reason, and job ID.
   - Keep plain text fallback.

2. Add notification quiet hours
   - Suppress non-critical success/skip messages during configured hours.
   - Always allow critical failures and emergency battery alerts.

3. Add notification channels abstraction
   - Keep Discord as first-class.
   - Make it possible to add Telegram, email, or Slack later without touching command logic.

## Weather And Forecasting

1. Use solar-relevant forecast fields
   - Add direct radiation or sunshine duration if available.
   - Use cloud cover plus precipitation as the fallback.

2. Add weather unavailable alert tuning
   - Alert only after repeated forecast failures, not on a single missed call.

## Testing And CI

1. Add tests for CLI dispatch boundaries
   - Verify each command maps to the intended command function.
   - Verify locked commands use `run_with_command_lock`.

2. Add tests for config loading
   - Mock environment variables.
   - Verify defaults and invalid values.
   - Ensure missing credentials produce the friendly error.

3. Add shell script checks
   - Use `shellcheck` for `.sh` files in CI if available.
   - Keep optional so contributors without shellcheck are not blocked locally.

4. Add pre-commit config
   - Check trailing whitespace, YAML validity, Python compile, and secret-looking values.

5. Add coverage reporting
   - Use coverage for tests without making coverage percentage a hard gate initially.

## Packaging And Developer Experience

1. Add `pyproject.toml`
   - Declare dependencies and package metadata.
   - Enable editable installs with `pip install -e .`.

2. Add console script entry point
   - Example command: `growatt-guard`.
   - Keep `growatt_power_guard.py` for backwards compatibility.

3. Add structured logging option
   - JSON logs can be easier to ship to external logging systems.
   - Keep current plain logs as default.

4. Add sample fixture data
   - Public-safe redacted Growatt status examples.
   - Helps tests and future API parsing work.

## Operations And Deployment

1. Add systemd status command
   - Example: `python growatt_power_guard.py service-status`.
   - Shows dashboard service, refresh timer, stale-alert timer, and cron presence.

2. Add one-shot VPS diagnostic bundle
   - Collect health-check, cron lines, service status, recent logs, and dashboard freshness.
   - Redact secrets.
   - Useful for support without exposing `.env`.

3. Add backup/restore for local state
   - Backup pause state, alert state, schedule overrides, and audit logs.
   - Keep generated logs out of Git.

## Security And Public Repo Hygiene

1. Add secret scanning helper
   - Local command/script that searches for `.env` style secrets, device IDs, webhooks, and exact coordinates.
   - Use it before public pushes.

2. Add webhook rotation notes
   - How to rotate a Discord webhook if it leaks.
   - How to test the new webhook.

## Larger Product Ideas

1. Multi-inverter support
   - Allow multiple device serials with per-device thresholds and schedules.
   - Requires careful audit and dashboard changes.

2. Local-first inverter integration
   - Explore whether a local dongle/API path can avoid Growatt cloud dependency.
   - Keep Growatt cloud as the current stable path unless local control is proven.

3. Web app with authenticated controls
   - A small authenticated control panel for pause/resume, schedule preview, and health checks.
   - Avoid live mode writes from the web until security is designed properly.

4. Forecast-based energy planner
   - Estimate expected solar charge before each outage.
   - Pick thresholds based on forecast, load history, and time to next utility window.

5. Estate schedule import
   - Parse a human-readable outage notice into schedule updates or proposed overrides.
   - Keep a manual confirmation step before changing cron.

## Good First Issues

1. Add tests for `growatt_guard/config.py`.
2. Add dashboard JSON output alongside `dashboard.html`.
3. Add `shellcheck` CI linting for `.sh` files.
4. Add `pyproject.toml` with dependency declarations.
5. Add public-safe fixture examples for Growatt status parsing.
6. Add notification quiet hours to suppress non-critical Discord messages overnight.
