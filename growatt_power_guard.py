from __future__ import annotations

import logging
from pathlib import Path

from growatt_guard.audit import (
    MODE_AUDIT_FILE,
    append_mode_audit,
    build_daily_summary,
    build_weekly_summary,
    read_mode_audit_rows,
    summarize_today_log_counts,
)
from growatt_guard.cli import (
    build_parser,
    dispatch_command,
    main,
    parse_command_tokens,
)
from growatt_guard.config import (
    Config,
    env,
    load_config,
    optional_float,
    str_to_bool,
)
from growatt_guard.dashboard import (
    DASHBOARD_FILE,
    MIN_DASHBOARD_REFRESH_MINUTES,
    command_dashboard,
    command_dashboard_refresh,
    command_dashboard_stale_alert,
    command_serve_dashboard,
    dashboard_freshness,
    read_dashboard_stale_alert_state,
)
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import (
    DeviceRef,
    describe_status_output_source,
    extract_soc,
    extract_spf_output_source,
    extract_status_soc,
    format_metric,
    load_context,
    render_params,
    set_mode,
    summarize_status,
    write_probe,
)
from growatt_guard.health import (
    HealthCheckItem,
    command_health_check,
    format_health_report,
    health_result,
)
from growatt_guard.modes import (
    command_battery_alert,
    command_daily_summary,
    command_morning_check,
    command_preserve_battery,
    command_probe,
    command_return_sbu,
    command_rotate_logs,
    command_run_scheduled,
    command_status,
    command_test_discord,
    command_utility_check,
    command_watchdog_sbu,
    command_weather_threshold,
    command_weekly_summary,
)
from growatt_guard.notifications import (
    notify_failure,
    read_growatt_cloud_failure_state,
    record_growatt_cloud_success,
    send_discord_message,
    truncate_discord_message,
)
from growatt_guard.pause import (
    command_pause,
    command_pause_status,
    command_resume,
    ensure_not_paused,
    run_with_command_lock,
)
from growatt_guard.schedule import (
    check_cron_schedule,
    command_schedule_preview,
    command_validate_schedule,
    find_schedule_job,
    next_scheduled_runs,
    schedule_job_tokens,
    today_schedule_override,
    validate_schedule,
    validate_schedule_overrides,
)
from growatt_guard.state import (
    acquire_command_lock,
    clear_battery_alert_state,
    clear_pause_state,
    command_lock_is_stale,
    format_local_time,
    pause_message,
    read_battery_alert_state,
    read_command_lock_state,
    read_pause_state,
    release_command_lock,
    utc_now,
    write_battery_alert_state,
    write_pause_state,
)
from growatt_guard.weather import (
    ThresholdDecision,
    analyze_weather_window,
    choose_preserve_threshold,
    fetch_weather_forecast,
)

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "growatt_power_guard.log"

PAUSABLE_COMMANDS = {"preserve-battery", "utility-check", "morning-check", "return-sbu", "watchdog-sbu"}
LOCKED_COMMANDS = PAUSABLE_COMMANDS


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    console_handler.setLevel(level)
    root.addHandler(console_handler)


if __name__ == "__main__":
    raise SystemExit(main())
