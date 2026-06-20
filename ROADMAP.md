# Roadmap And Enhancement Ideas

This is a parking lot for improvements that would make the Growatt guard safer, easier to operate, and more useful over time. Items are grouped by likely value and effort, not strict commitment.

## Highest Value Next

1. Split command implementations out of `growatt_power_guard.py`
   - Move mode commands into `growatt_guard/modes.py`.
   - Move health-check logic into `growatt_guard/health.py`.
   - Move pause/resume commands into `growatt_guard/pause.py`.
   - Goal: make `growatt_power_guard.py` a thin compatibility entry point.

2. Add a command simulation mode for scheduled jobs
   - Example: `python growatt_power_guard.py run-scheduled morning-preserve --dry-plan`.
   - Show which command would run, whether today's override applies, whether the command is paused, and whether it is mode-changing.
   - Useful before reinstalling cron.

3. Add schedule preview
   - Example: `python growatt_power_guard.py schedule-preview --days 7`.
   - Print upcoming jobs, date overrides, skipped jobs, and replacement commands.
   - Reuse `next_scheduled_runs` and override logic.

4. Make health-check more actionable
   - Include exact remediation hints for failed checks.
   - Include dashboard service status when running on Linux with systemd.
   - Include cron job count and next scheduled run summary.
   - Include whether `DRY_RUN` is enabled and whether any mode-changing job is currently paused.

5. Add dashboard sections for operations
   - Next 7 scheduled jobs.
   - Active pause/lock state.
   - Last successful Growatt read.
   - Last mode command result.
   - Recent failures and recovery streaks.
   - Current weather threshold reason when weather support is enabled.

## Safety And Reliability

1. Add stronger mode-write confirmation
   - After `set_mode`, wait briefly and re-read `outputConfig`.
   - Mark command as confirmed only when the status changes to the expected mode.
   - Send Discord warning if Growatt accepts the command but the mode does not verify.

2. Add retry policy for transient Growatt reads
   - Retry login/status calls on network timeouts and 5xx responses.
   - Use short backoff to avoid hammering the cloud API.
   - Keep mode writes conservative: do not blindly retry writes many times.

3. Add rate-limit guardrails
   - Track recent Growatt API calls in local state.
   - Refuse dashboard refresh intervals below the configured minimum.
   - Warn if new scheduled jobs would create unusually frequent cloud reads.

4. Add command idempotency checks
   - Before switching to Utility, skip if already Utility.
   - Before switching to SBU, skip if already SBU, except when `--force` is explicitly provided.
   - Still write an audit row for skipped idempotent actions.

5. Add emergency mode guard
   - If SOC is below an emergency threshold, prevent automatic SBU return unless explicitly allowed.
   - This would protect against returning to battery when the battery is critically low.
   - Needs careful user preference because outage windows may still require SBU.

6. Add stale lock recovery command
   - Example: `python growatt_power_guard.py clear-stale-lock`.
   - Only clears lock files older than the stale threshold.
   - Health-check can recommend it when needed.

## Scheduling Improvements

1. Add schedule override CLI
   - Commands like:
     - `schedule-skip --date YYYY-MM-DD --job morning-preserve --note "..."`
     - `schedule-skip-all --date YYYY-MM-DD --note "..."`
     - `schedule-replace --date YYYY-MM-DD --job morning-preserve --command health-check --arg --notify`
   - Writes `schedule_overrides.json` safely.

2. Add named outage profiles
   - Store profiles such as rainy-season, dry-season, holiday, maintenance.
   - Generate `schedule.json` from a profile instead of editing cron lines manually.

3. Add calendar export
   - Generate `.ics` calendar events for outage windows and automation jobs.
   - Useful for visually checking schedules before deployment.

4. Add schedule lint warnings
   - Warn if `return-sbu` is too close to an outage start.
   - Warn if preserve and return jobs overlap.
   - Warn if a mode-changing command runs too frequently.

5. Add dry-run cron installer mode
   - Show the exact crontab that would be installed.
   - Validate all job IDs before touching the user's crontab.

## Dashboard Enhancements

1. Serve JSON status alongside HTML
   - Generate `dashboard.json` with the same data used by `dashboard.html`.
   - Makes it easier to integrate with uptime monitors or other dashboards.

2. Add historical charts
   - SOC trend from audit/probe data.
   - Utility switches per day.
   - Watchdog repairs per week.
   - Growatt cloud failure streaks.

3. Add dashboard access hardening notes
   - Document recommended reverse proxy options.
   - Keep basic auth optional and avoid requiring paid services.
   - Include security reminders for public exposure.

4. Add manual action panel
   - Static dashboard could show copy-paste commands for pause, resume, health check, and dashboard refresh.
   - Avoid live write buttons unless authentication and CSRF protections are designed.

5. Add mobile-friendly compact mode
   - Smaller summary blocks for phone viewing.
   - Prioritize SOC, output mode, next job, pause state, and freshness.

## Notifications And Reporting

1. Add Discord embeds
   - Use structured fields for SOC, threshold, mode, reason, and job ID.
   - Keep plain text fallback.

2. Add notification quiet hours
   - Suppress non-critical success/skip messages during configured hours.
   - Always allow critical failures and emergency battery alerts.

3. Add weekly recommendations
   - Weekly summary could suggest threshold changes based on repeated no-change checks or frequent low-SOC events.
   - Example: "Afternoon preserve rarely switches; consider lowering normal threshold."

4. Add monthly report
   - Count utility switches, watchdog repairs, low-battery alerts, and Growatt cloud failures.
   - Include average preserve-check SOC and lowest observed SOC.

5. Add notification channels abstraction
   - Keep Discord as first-class.
   - Make it possible to add Telegram, email, or Slack later without touching command logic.

## Weather And Forecasting

1. Cache weather responses
   - Avoid repeated Open-Meteo calls if multiple commands run close together.
   - Store cache in `state/weather_cache.json` with a short TTL.

2. Use solar-relevant forecast fields
   - Add direct radiation or sunshine duration if available.
   - Use cloud cover plus precipitation as the fallback.

3. Add weather threshold preview
   - Show the next few forecast points and the selected threshold.
   - Useful for explaining why a command chose 50%, 45%, or 40%.

4. Add season profiles
   - Rainy season can default to 50%.
   - Dry season can default lower or use weather-aware thresholds more aggressively.

5. Add weather unavailable alert tuning
   - Alert only after repeated forecast failures.
   - Avoid noisy notifications from one missed weather call.

## Testing And CI

1. Split tests by module
   - Move tests into files such as `test_schedule.py`, `test_dashboard.py`, `test_growatt_api.py`, and `test_cli.py`.
   - Easier for agents to find the right test area.

2. Add tests for CLI dispatch boundaries
   - Verify each command maps to the intended command function.
   - Verify locked commands use `run_with_command_lock`.

3. Add tests for config loading
   - Mock environment variables.
   - Verify defaults and invalid values.
   - Ensure missing credentials produce the friendly error.

4. Add shell script checks
   - Use `shellcheck` for `.sh` files in CI if available.
   - Keep optional so contributors without shellcheck are not blocked locally.

5. Add pre-commit config
   - Check trailing whitespace, YAML validity, Python compile, and secret-looking values.

6. Add coverage reporting
   - Use coverage for tests without making coverage percentage a hard gate initially.

## Packaging And Developer Experience

1. Add `pyproject.toml`
   - Declare dependencies and package metadata.
   - Enable editable installs with `pip install -e .`.

2. Add console script entry point
   - Example command: `growatt-guard`.
   - Keep `growatt_power_guard.py` for backwards compatibility.

3. Add typed interfaces
   - Introduce small protocols for Growatt API/session objects.
   - Make mock tests clearer.

4. Add structured logging option
   - JSON logs can be easier to ship to external logging systems.
   - Keep current plain logs as default.

5. Add sample fixture data
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

4. Add update health gate
   - `update_server.sh` can run tests/compile/schedule validation before reinstalling cron.
   - If validation fails, leave current cron untouched.

5. Add post-deploy smoke commands
   - After `git pull`, run validate-schedule, health-check, dashboard-refresh --once, and dashboard-stale-alert.

## Security And Public Repo Hygiene

1. Add secret scanning helper
   - Local command/script that searches for `.env` style secrets, device IDs, webhooks, and exact coordinates.
   - Use it before public pushes.

2. Add docs for safe dashboard exposure
   - Explain risks of public dashboards.
   - Recommend auth, firewall, or private tunnel.
   - Keep domain examples generic.

3. Add permissions notes
   - Explain which files should be readable only by the automation user.
   - Especially `.env`, logs, and state files.

4. Add webhook rotation notes
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
2. Add `schedule-preview --days 7`.
3. Split tests into module-specific files.
4. Add weather response caching.
5. Add dashboard JSON output.
6. Add `clear-stale-lock`.
7. Add post-deploy smoke checks to `update_server.sh`.
8. Add monthly summary command.
9. Add CI shell script linting.
10. Add public-safe fixture examples for Growatt status parsing.
