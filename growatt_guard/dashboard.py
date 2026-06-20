from __future__ import annotations

import datetime as dt
import html
import http.server
import logging
import socketserver
import sys
import time
from pathlib import Path
from typing import Any

from growatt_guard.state import (
    clear_dashboard_stale_alert_state,
    pause_message,
    read_battery_alert_state,
    read_dashboard_stale_alert_state,
    read_growatt_cloud_failure_state,
    read_pause_state,
    utc_now,
    write_dashboard_stale_alert_state,
)


BASE_DIR = Path(__file__).resolve().parents[1]
DASHBOARD_FILE = BASE_DIR / "dashboard.html"
MIN_DASHBOARD_REFRESH_MINUTES = 5


def app_module() -> Any:
    module = sys.modules.get("growatt_power_guard")
    if module is not None and hasattr(module, "load_context"):
        return module

    main_module = sys.modules.get("__main__")
    if main_module is not None and hasattr(main_module, "load_context"):
        return main_module

    import growatt_power_guard

    return growatt_power_guard


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 60:
        unit = "second" if seconds == 1 else "seconds"
        return f"{seconds} {unit}"
    minutes = seconds // 60
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    unit = "hour" if hours == 1 else "hours"
    if remaining_minutes == 0:
        return f"{hours} {unit}"
    return f"{hours} {unit} {remaining_minutes} minutes"


def dashboard_freshness(
    output_path: Path,
    stale_minutes: float,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    if stale_minutes <= 0:
        raise app_module().GrowattGuardError("Dashboard stale threshold must be greater than 0 minutes.")

    now = now or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)

    if not output_path.exists():
        return {
            "path": str(output_path),
            "exists": False,
            "stale": True,
            "age_seconds": None,
            "modified_at": None,
            "stale_minutes": stale_minutes,
            "reason": "dashboard file does not exist",
        }

    modified_at = dt.datetime.fromtimestamp(output_path.stat().st_mtime, tz=dt.timezone.utc)
    age_seconds = max(0.0, (now - modified_at).total_seconds())
    stale = age_seconds > stale_minutes * 60
    age_text = format_duration(age_seconds)
    return {
        "path": str(output_path),
        "exists": True,
        "stale": stale,
        "age_seconds": age_seconds,
        "modified_at": modified_at.isoformat(),
        "stale_minutes": stale_minutes,
        "reason": (
            f"dashboard file is {age_text} old"
            if stale
            else f"dashboard file is fresh at {age_text} old"
        ),
    }


def build_dashboard_html(
    status: dict[str, Any],
    schedule: dict[str, Any],
    overrides: dict[str, Any],
    threshold_decision: Any,
    stale_after_minutes: float = 30,
) -> str:
    app = app_module()
    now = dt.datetime.now()
    generated_at = now.astimezone()
    generated_at_iso = generated_at.isoformat(timespec="seconds")
    soc_result = app.extract_soc(status)
    soc = f"{soc_result[0]:g}%" if soc_result else "Not found"
    output_source = app.extract_spf_output_source(status)
    mode = f"{output_source[1]} [{output_source[0]}]" if output_source else "Not found"
    pause_state = read_pause_state()
    pause = pause_message(pause_state) if pause_state else "active"
    alert_state = read_battery_alert_state()
    alert = "active" if alert_state and alert_state.get("active") else "clear"
    cloud_state = read_growatt_cloud_failure_state()
    cloud_streak = int(cloud_state.get("count", 0)) if cloud_state else 0
    today_override = app.today_schedule_override(overrides, now.date())
    override_note = str(today_override.get("note", "")).strip() or "none"
    skipped = ", ".join(today_override.get("skip", [])) if isinstance(today_override.get("skip", []), list) else ""
    last_actions = app.read_mode_audit_rows(limit=8, newest_first=True)
    next_runs = app.next_scheduled_runs(schedule, now=now, limit=8)
    stale_minutes_text = f"{stale_after_minutes:g}"

    next_rows = "\n".join(
        "<tr>"
        f"<td>{esc(run_at.strftime('%Y-%m-%d %H:%M'))}</td>"
        f"<td>{esc(job.get('id', ''))}</td>"
        f"<td>{esc(job.get('name', ''))}</td>"
        f"<td>{esc(' '.join(app.schedule_job_tokens(job)))}</td>"
        "</tr>"
        for run_at, job in next_runs
    )
    action_rows = "\n".join(
        "<tr>"
        f"<td>{esc(row.get('timestamp', ''))}</td>"
        f"<td>{esc(row.get('command', ''))}</td>"
        f"<td>{esc(row.get('action', ''))}</td>"
        f"<td>{esc(row.get('soc', ''))}</td>"
        f"<td>{esc(row.get('previous_mode', ''))}</td>"
        "</tr>"
        for row in last_actions
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>Growatt Dashboard</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; }}
    body {{ margin: 0; background: #f5f7f8; color: #172026; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    h1 {{ font-size: 28px; margin: 0 0 4px; }}
    h2 {{ font-size: 18px; margin: 28px 0 12px; }}
    .muted {{ color: #64727d; font-size: 14px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 20px; }}
    .card {{ background: #fff; border: 1px solid #dce3e8; border-radius: 8px; padding: 14px; }}
    .label {{ color: #64727d; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }}
    .value {{ font-size: 22px; font-weight: 700; margin-top: 8px; }}
    .small {{ font-size: 13px; margin-top: 8px; }}
    .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 9px; font-size: 14px; font-weight: 800; }}
    .badge-ok {{ background: #dff6e8; color: #155f34; }}
    .badge-warn {{ background: #fff2cc; color: #775800; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dce3e8; }}
    th, td {{ padding: 10px; border-bottom: 1px solid #e8eef2; text-align: left; font-size: 14px; }}
    th {{ background: #eef3f5; color: #34444f; }}
    tr:last-child td {{ border-bottom: 0; }}
  </style>
</head>
<body>
  <main>
    <h1>Growatt Dashboard</h1>
    <div class="muted">Generated {esc(generated_at.strftime('%Y-%m-%d %H:%M:%S %Z'))}</div>
    <section class="grid">
      <div class="card">
        <div class="label">Dashboard Health</div>
        <div class="value">
          <span class="badge badge-ok" data-refresh-badge data-generated-at="{esc(generated_at_iso)}" data-stale-minutes="{esc(stale_minutes_text)}">OK</span>
        </div>
        <div class="muted small" data-refresh-age>Generated just now; stale after {esc(stale_minutes_text)} minutes.</div>
      </div>
      <div class="card"><div class="label">Battery SOC</div><div class="value">{esc(soc)}</div></div>
      <div class="card"><div class="label">Output Source</div><div class="value">{esc(mode)}</div></div>
      <div class="card"><div class="label">Preserve Threshold</div><div class="value">{esc(f'{threshold_decision.threshold:g}%')}</div></div>
      <div class="card"><div class="label">Pause State</div><div class="value">{esc(pause)}</div></div>
      <div class="card"><div class="label">Emergency Alert</div><div class="value">{esc(alert)}</div></div>
      <div class="card"><div class="label">Cloud Streak</div><div class="value">{esc(cloud_streak)}</div></div>
      <div class="card"><div class="label">Today Override</div><div class="value">{esc(override_note)}</div></div>
    </section>
    <h2>Next Scheduled Jobs</h2>
    <table><thead><tr><th>Time</th><th>ID</th><th>Name</th><th>Command</th></tr></thead><tbody>{next_rows}</tbody></table>
    <h2>Recent Mode Decisions</h2>
    <table><thead><tr><th>Time</th><th>Command</th><th>Action</th><th>SOC</th><th>Previous Mode</th></tr></thead><tbody>{action_rows}</tbody></table>
    <h2>Automation Notes</h2>
    <div class="card">
      <div>Threshold: {esc(threshold_decision.reason)}</div>
      <div>Skipped today: {esc(skipped or 'none')}</div>
    </div>
  </main>
  <script>
    (function () {{
      const badge = document.querySelector("[data-refresh-badge]");
      const ageNode = document.querySelector("[data-refresh-age]");
      if (!badge || !ageNode) return;

      const generatedAt = new Date(badge.dataset.generatedAt);
      const staleMinutes = Number(badge.dataset.staleMinutes || "30");

      function plural(value, unit) {{
        return value + " " + unit + (value === 1 ? "" : "s");
      }}

      function formatAge(milliseconds) {{
        const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
        if (totalSeconds < 60) return plural(totalSeconds, "second");
        const totalMinutes = Math.floor(totalSeconds / 60);
        if (totalMinutes < 60) return plural(totalMinutes, "minute");
        const hours = Math.floor(totalMinutes / 60);
        const minutes = totalMinutes % 60;
        return minutes ? plural(hours, "hour") + " " + plural(minutes, "minute") : plural(hours, "hour");
      }}

      function updateRefreshHealth() {{
        if (Number.isNaN(generatedAt.getTime())) {{
          badge.textContent = "UNKNOWN";
          badge.className = "badge badge-warn";
          ageNode.textContent = "Generated time could not be read.";
          return;
        }}
        const ageMs = Date.now() - generatedAt.getTime();
        const stale = ageMs > staleMinutes * 60 * 1000;
        badge.textContent = stale ? "STALE" : "OK";
        badge.className = "badge " + (stale ? "badge-warn" : "badge-ok");
        ageNode.textContent = "Generated " + formatAge(ageMs) + " ago; stale after " + staleMinutes + " minutes.";
      }}

      updateRefreshHealth();
      window.setInterval(updateRefreshHealth, 30000);
    }})();
  </script>
</body>
</html>
"""


def resolve_dashboard_output(output: str) -> Path:
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = BASE_DIR / output_path
    return output_path


def write_dashboard(config: Any, output: str) -> Path:
    app = app_module()
    _, _, status = app.load_context(config)
    schedule = app.validate_schedule()
    overrides = app.validate_schedule_overrides(schedule)
    threshold_decision = app.choose_preserve_threshold(config)
    output_path = resolve_dashboard_output(output)
    output_path.write_text(
        build_dashboard_html(status, schedule, overrides, threshold_decision, config.dashboard_stale_minutes),
        encoding="utf-8",
    )
    return output_path


def command_dashboard(config: Any, output: str) -> int:
    output_path = write_dashboard(config, output)
    print(f"Wrote dashboard to {output_path}")
    return 0


def command_dashboard_refresh(config: Any, output: str, interval_minutes: float, once: bool = False) -> int:
    if not once and interval_minutes < MIN_DASHBOARD_REFRESH_MINUTES:
        raise app_module().GrowattGuardError(
            f"--interval-minutes must be at least {MIN_DASHBOARD_REFRESH_MINUTES} to avoid Growatt API overuse."
        )

    while True:
        try:
            output_path = write_dashboard(config, output)
        except Exception as exc:  # noqa: BLE001 - keep refresh service alive after transient failures
            logging.exception("Dashboard refresh failed")
            if once:
                raise
            app_module().notify_failure(config, "dashboard-refresh", str(exc))
        else:
            message = f"Dashboard refreshed: {output_path}"
            logging.info(message)
            print(message, flush=True)
            if once:
                return 0
        time.sleep(interval_minutes * 60)


def command_dashboard_stale_alert(config: Any, output: str, max_age_minutes: float | None = None) -> int:
    app = app_module()
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
                if not app.send_discord_message(config, message):
                    raise app.GrowattGuardError("Dashboard stale alert could not be sent to Discord.")
                state["notified"] = True
                state["last_alert_at"] = utc_now().isoformat()
                write_dashboard_stale_alert_state(state)
            print(f"Dashboard stale alert already active: {freshness['reason']}.")
            return 0

        notified = False
        if config.discord_webhook_url and config.discord_notify_failure:
            if not app.send_discord_message(config, message):
                raise app.GrowattGuardError("Dashboard stale alert could not be sent to Discord.")
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
            app.send_discord_message(config, message)
        print(f"Dashboard stale alert cleared: {freshness['reason']}.")
        return 0

    print(f"Dashboard freshness OK: {freshness['reason']}.")
    return 0


def make_dashboard_handler(output_path: Path):
    class DashboardHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path not in {"/", "/dashboard.html"}:
                self.send_error(404)
                return
            if not output_path.exists():
                body = (
                    "<!doctype html><html><body><h1>Growatt Dashboard</h1>"
                    "<p>Dashboard has not been generated yet.</p></body></html>"
                ).encode("utf-8")
                self.send_response(503)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = output_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
