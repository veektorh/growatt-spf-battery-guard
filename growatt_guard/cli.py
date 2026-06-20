from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from growatt_guard.config import Config, load_config
from growatt_guard.dashboard import DASHBOARD_FILE, MIN_DASHBOARD_REFRESH_MINUTES
from growatt_guard.notifications import notify_failure
from growatt_guard.schedule import command_validate_schedule


def app_module() -> Any:
    module = sys.modules.get("growatt_power_guard")
    if module is not None and hasattr(module, "GrowattGuardError"):
        return module

    main_module = sys.modules.get("__main__")
    if main_module is not None and hasattr(main_module, "GrowattGuardError"):
        return main_module

    import growatt_power_guard

    return growatt_power_guard


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Growatt SPF battery-preservation automation.")
    parser.add_argument("--verbose", action="store_true", help="Log extra details.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Log in, select plant/device, and print battery SOC.")
    subparsers.add_parser("probe", help="Write redacted raw Growatt responses to logs/ for setup.")
    subparsers.add_parser("preserve-battery", help="Switch to Utility if battery SOC is below LOW_BATTERY_SOC.")
    subparsers.add_parser("utility-check", help="Alias for preserve-battery.")
    subparsers.add_parser("morning-check", help="Alias for preserve-battery.")
    subparsers.add_parser("return-sbu", help="Switch back to SBU.")
    subparsers.add_parser("watchdog-sbu", help="Verify output source is SBU; retry SBU once if needed.")
    subparsers.add_parser("daily-summary", help="Post/print a daily Growatt and automation summary.")
    subparsers.add_parser("weekly-summary", help="Post/print a weekly automation performance summary.")
    subparsers.add_parser("rotate-logs", help="Delete old generated probe/log files according to LOG_RETENTION_DAYS.")
    subparsers.add_parser("weather-threshold", help="Print the current weather-aware preserve-battery threshold.")
    subparsers.add_parser("battery-alert", help="Send a Discord alert if battery SOC is below EMERGENCY_SOC.")
    subparsers.add_parser("validate-schedule", help="Validate schedule.json.")
    subparsers.add_parser("test-discord", help="Send a test Discord webhook message.")
    run_parser = subparsers.add_parser("run-scheduled", help="Run a schedule job by id, applying date overrides first.")
    run_parser.add_argument("job_id", help="Schedule job id from schedule.json.")
    run_parser.add_argument("--dry-plan", action="store_true", help="Print what would happen without running anything.")
    health_parser = subparsers.add_parser("health-check", help="Run read-only configuration and connectivity checks.")
    health_parser.add_argument("--notify", action="store_true", help="Post the health report to Discord.")
    dashboard_parser = subparsers.add_parser("dashboard", help="Generate a small local HTML dashboard.")
    dashboard_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML output path.")
    refresh_parser = subparsers.add_parser("dashboard-refresh", help="Regenerate dashboard.html on a safe interval.")
    refresh_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML output path.")
    refresh_parser.add_argument(
        "--interval-minutes",
        type=float,
        default=10,
        help=f"Refresh interval. Minimum {MIN_DASHBOARD_REFRESH_MINUTES} minutes unless --once is used.",
    )
    refresh_parser.add_argument("--once", action="store_true", help="Refresh once, then exit.")
    stale_parser = subparsers.add_parser("dashboard-stale-alert", help="Alert if dashboard.html has not refreshed recently.")
    stale_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML file to check.")
    stale_parser.add_argument(
        "--max-age-minutes",
        type=float,
        default=None,
        help="Override DASHBOARD_STALE_MINUTES for this check.",
    )
    serve_parser = subparsers.add_parser("serve-dashboard", help="Serve dashboard.html without calling Growatt.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host. Use 127.0.0.1 for SSH tunnel access.")
    serve_parser.add_argument("--port", type=int, default=8080, help="Bind port.")
    serve_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML file to serve.")
    pause_parser = subparsers.add_parser("pause", help="Pause scheduled mode-changing automation.")
    pause_parser.add_argument("--hours", type=float, required=True, help="How long to pause automation for.")
    pause_parser.add_argument("--reason", default="", help="Optional reason stored in pause state and Discord alert.")
    subparsers.add_parser("resume", help="Resume scheduled mode-changing automation.")
    subparsers.add_parser("pause-status", help="Show whether automation is currently paused.")
    subparsers.add_parser("clear-stale-lock", help="Remove a stale mode-command lock file if one exists.")
    preview_parser = subparsers.add_parser(
        "schedule-preview", help="Print upcoming scheduled jobs for the next N days, including overrides."
    )
    preview_parser.add_argument("--days", type=int, default=7, help="Number of days to preview (default 7).")
    return parser


def parse_command_tokens(tokens: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(tokens)


def dispatch_command(config: Config, args: argparse.Namespace) -> int:
    app = app_module()
    command = args.command

    def action() -> int:
        if command == "status":
            return app.command_status(config)
        if command == "probe":
            return app.command_probe(config)
        if command == "preserve-battery":
            return app.command_preserve_battery(config)
        if command == "utility-check":
            return app.command_utility_check(config)
        if command == "morning-check":
            return app.command_morning_check(config)
        if command == "return-sbu":
            return app.command_return_sbu(config)
        if command == "watchdog-sbu":
            return app.command_watchdog_sbu(config)
        if command == "daily-summary":
            return app.command_daily_summary(config)
        if command == "weekly-summary":
            return app.command_weekly_summary(config)
        if command == "rotate-logs":
            return app.command_rotate_logs(config)
        if command == "weather-threshold":
            return app.command_weather_threshold(config)
        if command == "battery-alert":
            return app.command_battery_alert(config)
        if command == "test-discord":
            return app.command_test_discord(config)
        if command == "health-check":
            return app.command_health_check(config, args.notify)
        if command == "dashboard":
            return app.command_dashboard(config, args.output)
        if command == "dashboard-refresh":
            return app.command_dashboard_refresh(config, args.output, args.interval_minutes, args.once)
        if command == "dashboard-stale-alert":
            return app.command_dashboard_stale_alert(config, args.output, args.max_age_minutes)
        if command == "serve-dashboard":
            return app.command_serve_dashboard(config, args.host, args.port, args.output)
        if command == "run-scheduled":
            return app.command_run_scheduled(config, args.job_id, dry_plan=args.dry_plan)
        if command == "pause":
            return app.command_pause(config, args.hours, args.reason)
        if command == "resume":
            return app.command_resume(config)
        if command == "pause-status":
            return app.command_pause_status(config)
        if command == "clear-stale-lock":
            return app.command_clear_stale_lock(config)
        if command == "schedule-preview":
            return app.command_schedule_preview(config, args.days)
        raise app.GrowattGuardError(f"Unknown command: {command}")

    if command in app.LOCKED_COMMANDS:
        return app.run_with_command_lock(config, command, action)
    return action()


def main(argv: list[str] | None = None) -> int:
    app = app_module()
    parser = build_parser()
    args = parser.parse_args(argv)
    app.setup_logging(args.verbose)
    config: Config | None = None

    try:
        if args.command == "validate-schedule":
            return command_validate_schedule()
        config = load_config()
        logging.info("Command=%s dry_run=%s low_soc=%s", args.command, config.dry_run, config.low_battery_soc)
        return dispatch_command(config, args)
    except app.GrowattGuardError as exc:
        logging.error("%s", exc)
        notify_failure(config, args.command, str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - logs traceback for unattended scheduler runs
        logging.exception("Unhandled error")
        notify_failure(config, args.command, str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
