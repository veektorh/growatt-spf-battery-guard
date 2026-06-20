from __future__ import annotations

import logging
from pathlib import Path

from growatt_guard.audit import (
    MODE_AUDIT_FILE,
    append_mode_audit,
    build_chart_data,
    build_daily_summary,
    build_monthly_summary,
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
    command_observability_refresh,
    command_serve_dashboard,
    dashboard_freshness,
    read_dashboard_stale_alert_state,
    refresh_observability_once,
    write_dashboard_from_status,
)
from growatt_guard.discord_control import (
    command_result_text,
    command_serve_discord_bot,
    is_authorized_interaction,
    trim_output,
    validate_control_config,
)
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import (
    DeviceRef,
    SPF_EXPECTED_OUTPUT_CONFIG,
    describe_status_output_source,
    extract_soc,
    extract_spf_output_source,
    extract_status_soc,
    format_metric,
    load_context,
    render_params,
    set_mode,
    summarize_status,
    verify_mode_switch,
    write_probe,
)
from growatt_guard.health import (
    HealthCheckItem,
    command_health_check,
    format_health_report,
    health_result,
)
from growatt_guard.modes import (
    command_auto_topup_check,
    command_battery_alert,
    command_daily_summary,
    command_estimate_charge_rate,
    command_force_utility,
    command_monthly_summary,
    command_morning_check,
    command_preserve_battery,
    command_probe,
    command_return_sbu,
    command_rotate_logs,
    command_run_scheduled,
    command_runtime_alert,
    command_status,
    command_test_discord,
    command_topup_complete_check,
    command_utility_check,
    command_watchdog_sbu,
    command_weather_threshold,
    command_weekly_summary,
)
from growatt_guard.pvoutput import (
    PVOUTPUT_STATE_FILE,
    PVOUTPUT_URL,
    command_pvoutput_upload,
    extract_pvoutput_fields,
    publish_pvoutput_status_from_status,
    read_pvoutput_state,
    upload_pvoutput_status,
    write_pvoutput_state,
)
from growatt_guard.notifications import (
    notify_failure,
    read_growatt_cloud_failure_state,
    record_growatt_cloud_success,
    send_discord_message,
    truncate_discord_message,
)
from growatt_guard.pause import (
    command_clear_stale_lock,
    command_pause,
    command_pause_status,
    command_resume,
    ensure_not_paused,
    run_with_command_lock,
)
from growatt_guard.schedule import (
    BUILTIN_OUTAGE_PROFILES,
    check_cron_schedule,
    command_outage_profile,
    command_schedule_override,
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
    clear_topup_state,
    command_lock_is_stale,
    format_local_time,
    pause_message,
    read_battery_alert_state,
    read_command_lock_state,
    read_pause_state,
    read_topup_state,
    release_command_lock,
    topup_is_active,
    utc_now,
    write_battery_alert_state,
    write_pause_state,
    write_topup_state,
)
from growatt_guard.weather import (
    DRY_SEASON_THRESHOLDS,
    RAINY_SEASON_MONTHS,
    ThresholdDecision,
    analyze_weather_window,
    apply_season_adjustment,
    choose_preserve_threshold,
    current_season,
    fetch_weather_forecast,
)

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "growatt_power_guard.log"

PAUSABLE_COMMANDS = {"preserve-battery", "utility-check", "morning-check", "return-sbu", "watchdog-sbu"}
LOCKED_COMMANDS = PAUSABLE_COMMANDS | {"force-utility"}


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
