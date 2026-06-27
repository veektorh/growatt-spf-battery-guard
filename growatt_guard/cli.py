from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from growatt_guard.config import Config, load_config, validate_config
from growatt_guard.dashboard import DASHBOARD_FILE, MIN_DASHBOARD_REFRESH_MINUTES
from growatt_guard.diagnostics import (
    command_diagnostic_bundle,
    command_pv_metric_probe,
    command_redact_probe,
    command_service_status,
)
from growatt_guard.discord_control import command_serve_discord_bot
from growatt_guard.notifications import notify_failure
from growatt_guard.pvoutput import command_pvoutput_upload
from growatt_guard.schedule import command_outage_profile, command_schedule_override, command_validate_schedule


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
    force_utility_parser = subparsers.add_parser("force-utility", help="Switch to Utility first without an SOC threshold.")
    force_utility_parser.add_argument("--reason", default="", help="Optional reason stored in the audit log.")
    subparsers.add_parser("return-sbu", help="Switch back to SBU.")
    subparsers.add_parser("watchdog-sbu", help="Verify output source is SBU; retry SBU once if needed.")
    subparsers.add_parser("daily-summary", help="Post/print a daily Growatt and automation summary.")
    subparsers.add_parser("weekly-summary", help="Post/print a weekly automation performance summary.")
    subparsers.add_parser("monthly-summary", help="Post/print a 30-day automation performance summary.")
    subparsers.add_parser("rotate-logs", help="Delete old generated probe/log files according to LOG_RETENTION_DAYS.")
    subparsers.add_parser("prune-audit", help="Remove audit CSV rows older than AUDIT_RETENTION_DAYS (default 90).")
    subparsers.add_parser("weather-threshold", help="Print the current weather-aware preserve-battery threshold.")
    subparsers.add_parser("battery-alert", help="Send a Discord alert if battery SOC is below EMERGENCY_SOC.")
    subparsers.add_parser("validate-schedule", help="Validate schedule.json.")
    subparsers.add_parser("test-discord", help="Send a test Discord webhook message.")
    run_parser = subparsers.add_parser("run-scheduled", help="Run a schedule job by id, applying date overrides first.")
    run_parser.add_argument("job_id", help="Schedule job id from schedule.json.")
    run_parser.add_argument("--dry-plan", action="store_true", help="Print what would happen without running anything.")
    health_parser = subparsers.add_parser("health-check", help="Run read-only configuration and connectivity checks.")
    health_parser.add_argument("--notify", action="store_true", help="Post the health report to Discord.")
    service_parser = subparsers.add_parser("service-status", help="Show local cron, systemd, dashboard, pause, and topup status.")
    service_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    bundle_parser = subparsers.add_parser("diagnostic-bundle", help="Print a redacted local diagnostics bundle.")
    bundle_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    bundle_parser.add_argument("--include-cloud", action="store_true", help="Include one read-only Growatt cloud summary.")
    pv_probe_parser = subparsers.add_parser("pv-metric-probe", help="Print redacted PV metric paths and parsed dashboard values.")
    pv_probe_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    redact_probe_parser = subparsers.add_parser("redact-probe", help="Redact a raw JSON probe for fixture use.")
    redact_probe_parser.add_argument("input", help="Raw JSON probe file.")
    redact_probe_parser.add_argument("--output", default="", help="Optional redacted JSON output file.")
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
    observability_parser = subparsers.add_parser(
        "observability-refresh",
        help="Read Growatt once, refresh dashboard.html, and upload PVOutput if enabled.",
    )
    observability_parser.add_argument("--output", default=str(DASHBOARD_FILE), help="Dashboard HTML output path.")
    observability_parser.add_argument(
        "--interval-minutes",
        type=float,
        default=10,
        help=f"Loop interval when --loop is used. Minimum {MIN_DASHBOARD_REFRESH_MINUTES} minutes.",
    )
    observability_parser.add_argument("--loop", action="store_true", help="Keep refreshing on the configured interval.")
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
    subparsers.add_parser("serve-discord-bot", help="Run the private Discord control bot.")
    pause_parser = subparsers.add_parser("pause", help="Pause scheduled mode-changing automation.")
    pause_parser.add_argument("--hours", type=float, required=True, help="How long to pause automation for.")
    pause_parser.add_argument("--reason", default="", help="Optional reason stored in pause state and Discord alert.")
    subparsers.add_parser("resume", help="Resume scheduled mode-changing automation.")
    subparsers.add_parser("pause-status", help="Show whether automation is currently paused.")
    subparsers.add_parser("clear-stale-lock", help="Remove a stale mode-command lock file if one exists.")
    subparsers.add_parser("clear-login-cooldown", help="Clear the Growatt login cooldown set after an account lock (507).")
    preview_parser = subparsers.add_parser(
        "schedule-preview", help="Print upcoming scheduled jobs for the next N days, including overrides."
    )
    preview_parser.add_argument("--days", type=int, default=7, help="Number of days to preview (default 7).")
    preview_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    override_parser = subparsers.add_parser(
        "schedule-override", help="Manage temporary date overrides in schedule_overrides.json."
    )
    override_sub = override_parser.add_subparsers(dest="override_subcommand", required=True)

    override_list = override_sub.add_parser("list", help="List current overrides.")
    override_list.add_argument("date", nargs="?", default="", help="Filter to a specific YYYY-MM-DD date.")

    override_add_skip = override_sub.add_parser("add-skip", help="Skip a job on a specific date.")
    override_add_skip.add_argument("date", help="YYYY-MM-DD date.")
    override_add_skip.add_argument("job_id", help="Schedule job id to skip.")
    override_add_skip.add_argument("--note", default="", help="Optional reason note.")

    override_add_skip_all = override_sub.add_parser("add-skip-all", help="Skip all jobs on a specific date.")
    override_add_skip_all.add_argument("date", help="YYYY-MM-DD date.")
    override_add_skip_all.add_argument("--note", default="", help="Optional reason note.")

    override_add_replace = override_sub.add_parser("add-replace", help="Replace a job with another command on a specific date.")
    override_add_replace.add_argument("date", help="YYYY-MM-DD date.")
    override_add_replace.add_argument("job_id", help="Schedule job id to replace.")
    override_add_replace.add_argument("replacement_command", help="Command to run instead.")
    override_add_replace.add_argument("--note", default="", help="Optional reason note.")
    override_add_replace.add_argument(
        "replacement_args",
        nargs=argparse.REMAINDER,
        help="Args for the replacement command (e.g. --notify). Put --note before these.",
    )

    override_remove = override_sub.add_parser("remove", help="Remove overrides for a date (or a specific job on that date).")
    override_remove.add_argument("date", help="YYYY-MM-DD date.")
    override_remove.add_argument(
        "job_id", nargs="?", default="", help="Job id to remove. If omitted, removes all overrides for the date."
    )

    outage_parser = subparsers.add_parser(
        "outage-profile", help="Apply a named outage profile (skip-all, maintenance, health-only) to one or more dates."
    )
    outage_sub = outage_parser.add_subparsers(dest="outage_subcommand", required=True)

    outage_sub.add_parser("list", help="List available outage profiles.")

    outage_apply = outage_sub.add_parser("apply", help="Apply a profile to one or more dates.")
    outage_apply.add_argument("profile_name", help="Profile name (skip-all, maintenance, health-only).")
    outage_apply.add_argument("dates", nargs="+", help="YYYY-MM-DD date(s) to apply the profile to.")
    outage_apply.add_argument("--note", default="", help="Optional note stored with each override.")

    subparsers.add_parser(
        "pvoutput-upload",
        help="Upload the current Growatt status to PVOutput.org (requires PVOUTPUT_ENABLED=true).",
    )

    charge_rate_parser = subparsers.add_parser(
        "estimate-charge-rate",
        help="Read SOC before and after a wait while charging to estimate BATTERY_CHARGE_RATE_W.",
    )
    charge_rate_parser.add_argument(
        "--wait-seconds",
        type=int,
        default=900,
        help="Seconds to wait between readings (default 900 = 15 min). Longer gives a more accurate result.",
    )

    subparsers.add_parser(
        "auto-topup-check",
        help="Check if battery will survive until sunrise; if not, start a timed Utility top-up and exit.",
    )
    subparsers.add_parser(
        "topup-complete-check",
        help="Check if an auto-topup has finished; if so, resume automation and return to SBU.",
    )
    subparsers.add_parser(
        "runtime-alert",
        help="Send a Discord alert if estimated battery runtime is below RUNTIME_ALERT_MINUTES.",
    )

    adopt_parser = subparsers.add_parser(
        "adopt-utility",
        help="Adopt the current Utility state and auto-return to SBU at target SOC%%.",
    )
    adopt_parser.add_argument(
        "target_soc", type=float, help="Target battery SOC percentage to charge to before returning to SBU."
    )

    snooze_parser = subparsers.add_parser(
        "snooze-waste",
        help="Snooze waste-alert-check notifications. Duration: '2h', '30m', or 'today'.",
    )
    snooze_parser.add_argument("duration", help="Snooze duration: '2h', '30m', or 'today'.")

    subparsers.add_parser(
        "waste-alert-check",
        help=(
            "Notify if Utility is on during daylight, PV can cover load, "
            "and Growatt Guard has no active hold."
        ),
    )

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
        if command == "force-utility":
            return app.command_force_utility(config, args.reason)
        if command == "return-sbu":
            return app.command_return_sbu(config)
        if command == "watchdog-sbu":
            return app.command_watchdog_sbu(config)
        if command == "daily-summary":
            return app.command_daily_summary(config)
        if command == "weekly-summary":
            return app.command_weekly_summary(config)
        if command == "monthly-summary":
            return app.command_monthly_summary(config)
        if command == "rotate-logs":
            return app.command_rotate_logs(config)
        if command == "prune-audit":
            return app.command_prune_audit(config)
        if command == "weather-threshold":
            return app.command_weather_threshold(config)
        if command == "battery-alert":
            return app.command_battery_alert(config)
        if command == "test-discord":
            return app.command_test_discord(config)
        if command == "health-check":
            return app.command_health_check(config, args.notify)
        if command == "service-status":
            return command_service_status(config, args.json)
        if command == "diagnostic-bundle":
            return command_diagnostic_bundle(config, args.json, args.include_cloud)
        if command == "pv-metric-probe":
            return command_pv_metric_probe(config, args.json)
        if command == "redact-probe":
            return command_redact_probe(args.input, args.output)
        if command == "dashboard":
            return app.command_dashboard(config, args.output)
        if command == "dashboard-refresh":
            return app.command_dashboard_refresh(config, args.output, args.interval_minutes, args.once)
        if command == "observability-refresh":
            return app.command_observability_refresh(config, args.output, args.interval_minutes, args.loop)
        if command == "dashboard-stale-alert":
            return app.command_dashboard_stale_alert(config, args.output, args.max_age_minutes)
        if command == "serve-dashboard":
            return app.command_serve_dashboard(config, args.host, args.port, args.output)
        if command == "serve-discord-bot":
            return command_serve_discord_bot(config)
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
        if command == "clear-login-cooldown":
            return app.command_clear_login_cooldown(config)
        if command == "schedule-preview":
            return app.command_schedule_preview(config, args.days, json_output=args.json)
        if command == "schedule-override":
            return command_schedule_override(config, args)
        if command == "outage-profile":
            return command_outage_profile(config, args)
        if command == "pvoutput-upload":
            return command_pvoutput_upload(config)
        if command == "estimate-charge-rate":
            return app.command_estimate_charge_rate(config, args.wait_seconds)
        if command == "auto-topup-check":
            return app.command_auto_topup_check(config)
        if command == "topup-complete-check":
            return app.command_topup_complete_check(config)
        if command == "runtime-alert":
            return app.command_runtime_alert(config)
        if command == "adopt-utility":
            return app.command_adopt_utility(config, args.target_soc)
        if command == "snooze-waste":
            return app.command_snooze_waste(config, args.duration)
        if command == "waste-alert-check":
            return app.command_waste_alert_check(config)
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
        if args.command == "redact-probe":
            return command_redact_probe(args.input, args.output)
        config = load_config()
        for _w in validate_config(config):
            logging.warning("Config: %s", _w)
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
