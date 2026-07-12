from __future__ import annotations

import datetime as dt
import http.server
import json
import logging
import socketserver
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from growatt_guard.dashboard import (
    BASE_DIR,
    MIN_DASHBOARD_REFRESH_MINUTES,
    append_dashboard_metric_snapshot,
    build_dashboard_data_payload,
    build_dashboard_html,
    dashboard_freshness,
    read_dashboard_metrics_history,
)
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.forecast_calibration import update_forecast_calibration
from growatt_guard.growatt_api import load_context
from growatt_guard.notifications import notify_failure, send_discord_message
from growatt_guard.pvoutput import publish_pvoutput_status_from_status
from growatt_guard.schedule import validate_schedule, validate_schedule_overrides
from growatt_guard.state import (
    clear_dashboard_stale_alert_state,
    read_dashboard_stale_alert_state,
    utc_now,
    write_dashboard_stale_alert_state,
)
from growatt_guard.topup_status import collect_topup_status
from growatt_guard.weather import (
    choose_preserve_threshold,
    get_pv_forecast,
    hours_until_next_sunrise,
)

def resolve_dashboard_output(output: str) -> Path:
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = BASE_DIR / output_path
    return output_path


def resolve_dashboard_json_output(output_path: Path) -> Path:
    return output_path.with_suffix(".json")


def _write_json_atomic(output_path: Path, payload: dict[str, Any]) -> None:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output_path.parent,
        prefix=".dash_tmp_", suffix=".json", delete=False,
    )
    try:
        json.dump(payload, tmp, indent=2, sort_keys=True, default=lambda o: o.isoformat() if isinstance(o, (dt.datetime, dt.date)) else str(o))
        tmp.write("\n")
        tmp.flush()
        tmp.close()
        Path(tmp.name).replace(output_path)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def write_dashboard_from_status(config: Any, status: dict[str, Any], output: str) -> Path:
    from growatt_guard.weather import hours_until_next_sunset
    from growatt_guard.state import read_utility_hold_state
    schedule = validate_schedule()
    overrides = validate_schedule_overrides(schedule)
    threshold_decision = choose_preserve_threshold(config)
    hrs_to_sunrise: float | None = None
    hrs_to_sunset: float | None = None
    try:
        hrs_to_sunrise = hours_until_next_sunrise(config)
    except Exception:  # noqa: BLE001
        pass
    try:
        hrs_to_sunset = hours_until_next_sunset(config)
    except Exception:  # noqa: BLE001
        pass
    output_path = resolve_dashboard_output(output)
    append_dashboard_metric_snapshot(status, now=dt.datetime.now().astimezone())
    metrics_history = read_dashboard_metrics_history()
    pv_forecast = get_pv_forecast(config)
    topup_status = collect_topup_status(config, status=status)
    calibration = update_forecast_calibration(
        pv_forecast,
        metrics_history,
        current_performance_ratio=config.panel_performance_ratio,
        sunny_threshold_kwh_m2=config.auto_topup_solar_skip_kwh_m2,
    )
    if pv_forecast is not None:
        pv_forecast["calibration"] = calibration
    json_payload = build_dashboard_data_payload(
        status, schedule, overrides, threshold_decision, config.dashboard_stale_minutes,
        config.battery_capacity_wh, config.battery_bms_cutoff_soc,
        hrs_to_sunrise, config.battery_charge_rate_w,
        config.auto_topup_target_soc,
        config.auto_topup_solar_skip_min_margin_minutes,
        metrics_history,
        hours_to_sunset=hrs_to_sunset,
        pv_forecast=pv_forecast,
        min_sbu_return_soc=config.min_sbu_return_soc,
        topup_status=topup_status,
    )
    html_content = build_dashboard_html(
        status, schedule, overrides, threshold_decision, config.dashboard_stale_minutes,
        config.battery_capacity_wh, config.battery_bms_cutoff_soc,
        hrs_to_sunrise, config.battery_charge_rate_w,
        config.auto_topup_target_soc,
        config.auto_topup_solar_skip_min_margin_minutes,
        config.auto_topup_min_minutes,
        config.discord_topup_max_minutes,
        metrics_history,
        hours_to_sunset=hrs_to_sunset,
        tonight_floor_soc=config.auto_topup_sunrise_floor_soc,
        tonight_comfortable_soc=45.0,
        utility_hold_state=read_utility_hold_state(),
        pv_forecast=pv_forecast,
        min_sbu_return_soc=config.min_sbu_return_soc,
        dashboard_data=json_payload,
    )
    # Atomic write: temp file in same directory then rename to avoid serving
    # a partially written file when the browser auto-refreshes mid-write.
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output_path.parent,
        prefix=".dash_tmp_", suffix=".html", delete=False,
    )
    try:
        tmp.write(html_content)
        tmp.flush()
        tmp.close()
        Path(tmp.name).replace(output_path)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    _write_json_atomic(resolve_dashboard_json_output(output_path), json_payload)
    return output_path


def write_dashboard(config: Any, output: str) -> Path:
    _, _, status = load_context(config)
    return write_dashboard_from_status(config, status, output)


def command_dashboard(config: Any, output: str) -> int:
    output_path = write_dashboard(config, output)
    print(f"Wrote dashboard to {output_path}")
    return 0


def command_dashboard_refresh(config: Any, output: str, interval_minutes: float, once: bool = False) -> int:
    if not once and interval_minutes < MIN_DASHBOARD_REFRESH_MINUTES:
        raise GrowattGuardError(
            f"--interval-minutes must be at least {MIN_DASHBOARD_REFRESH_MINUTES} to avoid Growatt API overuse."
        )

    while True:
        try:
            output_path = write_dashboard(config, output)
        except Exception as exc:  # noqa: BLE001 - keep refresh service alive after transient failures
            logging.exception("Dashboard refresh failed")
            if once:
                raise
            notify_failure(config, "dashboard-refresh", str(exc))
        else:
            message = f"Dashboard refreshed: {output_path}"
            logging.info(message)
            print(message, flush=True)
            if once:
                return 0
        time.sleep(interval_minutes * 60)


def refresh_observability_once(config: Any, output: str) -> dict[str, Any]:
    _, _, status = load_context(config)
    output_path = write_dashboard_from_status(config, status, output)
    try:
        pvoutput_ok, pvoutput_message = publish_pvoutput_status_from_status(config, status)
    except Exception as exc:  # noqa: BLE001 - dashboard refresh should survive PVOutput issues
        logging.exception("PVOutput step failed during observability refresh")
        pvoutput_ok = False
        pvoutput_message = f"PVOutput failed: {exc}"
    return {
        "dashboard_path": output_path,
        "pvoutput_ok": pvoutput_ok,
        "pvoutput_message": pvoutput_message,
    }


def command_observability_refresh(config: Any, output: str, interval_minutes: float, loop: bool = False) -> int:
    if loop and interval_minutes < MIN_DASHBOARD_REFRESH_MINUTES:
        raise GrowattGuardError(
            f"--interval-minutes must be at least {MIN_DASHBOARD_REFRESH_MINUTES} to avoid Growatt API overuse."
        )

    while True:
        try:
            result = refresh_observability_once(config, output)
        except Exception as exc:  # noqa: BLE001 - keep loop service alive after transient failures
            logging.exception("Observability refresh failed")
            if not loop:
                raise
            notify_failure(config, "observability-refresh", str(exc))
        else:
            message = (
                f"Observability refreshed: dashboard={result['dashboard_path']}; "
                f"{result['pvoutput_message']}"
            )
            logging.info(message)
            print(message, flush=True)
            if not result["pvoutput_ok"]:
                logging.error("%s", result["pvoutput_message"])
                if not loop:
                    raise GrowattGuardError(str(result["pvoutput_message"]))
                notify_failure(config, "observability-refresh", str(result["pvoutput_message"]))
            if not loop:
                return 0
        time.sleep(interval_minutes * 60)


def command_dashboard_stale_alert(config: Any, output: str, max_age_minutes: float | None = None) -> int:
    stale_minutes = max_age_minutes if max_age_minutes is not None else config.dashboard_stale_minutes
    output_path = resolve_dashboard_output(output)
    freshness = dashboard_freshness(output_path, stale_minutes)
    state = read_dashboard_stale_alert_state()

    if freshness["stale"]:
        message = (
            "Growatt dashboard refresh is stale.\n"
            f"Dashboard file: `{freshness['path']}`.\n"
            f"Reason: {freshness['reason']}.\n"
            f"Stale threshold: `{stale_minutes:g}` minutes."
        )
        if state and state.get("active"):
            if not state.get("notified") and config.discord_webhook_url and config.discord_notify_failure:
                if not send_discord_message(config, message):
                    raise GrowattGuardError("Dashboard stale alert could not be sent to Discord.")
                state["notified"] = True
                state["last_alert_at"] = utc_now().isoformat()
                write_dashboard_stale_alert_state(state)
            print(f"Dashboard stale alert already active: {freshness['reason']}.")
            return 0

        notified = False
        if config.discord_webhook_url and config.discord_notify_failure:
            if not send_discord_message(config, message):
                raise GrowattGuardError("Dashboard stale alert could not be sent to Discord.")
            notified = True

        write_dashboard_stale_alert_state(
            {
                "active": True,
                "notified": notified,
                "first_detected_at": utc_now().isoformat(),
                "last_alert_at": utc_now().isoformat() if notified else "",
                "path": freshness["path"],
                "reason": freshness["reason"],
                "stale_minutes": stale_minutes,
            }
        )
        print(f"Dashboard stale alert {'sent' if notified else 'recorded'}: {freshness['reason']}.")
        return 0

    if state and state.get("active"):
        clear_dashboard_stale_alert_state()
        message = (
            "Growatt dashboard refresh recovered.\n"
            f"Dashboard file is fresh again: {freshness['reason']}."
        )
        if state.get("notified") and config.discord_webhook_url and config.discord_notify_failure:
            send_discord_message(config, message)
        print(f"Dashboard stale alert cleared: {freshness['reason']}.")
        return 0

    print(f"Dashboard freshness OK: {freshness['reason']}.")
    return 0


def dashboard_asset_for_path(output_path: Path, request_path: str) -> tuple[int, str, bytes] | None:
    parsed_path = urllib.parse.urlsplit(request_path).path
    if parsed_path in {"/", "/dashboard.html"}:
        if not output_path.exists():
            body = (
                "<!doctype html><html><body><h1>Growatt Dashboard</h1>"
                "<p>Dashboard has not been generated yet.</p></body></html>"
            ).encode("utf-8")
            return 503, "text/html; charset=utf-8", body
        return 200, "text/html; charset=utf-8", output_path.read_bytes()

    if parsed_path == "/dashboard.json":
        json_path = resolve_dashboard_json_output(output_path)
        if not json_path.exists():
            body = json.dumps(
                {
                    "error": "dashboard_json_not_generated",
                    "message": "dashboard.json has not been generated yet.",
                },
                separators=(",", ":"),
            ).encode("utf-8")
            return 503, "application/json; charset=utf-8", body
        return 200, "application/json; charset=utf-8", json_path.read_bytes()

    return None


def make_dashboard_handler(output_path: Path):
    class DashboardHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            asset = dashboard_asset_for_path(output_path, self.path)
            if asset is None:
                self.send_error(404)
                return
            status_code, content_type, body = asset
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler API
            logging.info("Dashboard server: " + format, *args)

    return DashboardHandler


def command_serve_dashboard(config: Any, host: str, port: int, output: str) -> int:
    _ = config
    output_path = resolve_dashboard_output(output)
    handler = make_dashboard_handler(output_path)

    class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    with ReusableThreadingTCPServer((host, port), handler) as server:
        print(f"Serving {output_path} at http://{host}:{port}/dashboard.html", flush=True)
        server.serve_forever()
    return 0
