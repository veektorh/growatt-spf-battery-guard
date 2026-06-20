from __future__ import annotations

import datetime as dt
import html
import http.server
import json
import logging
import socketserver
import sys
import time
from pathlib import Path
from typing import Any

from growatt_guard.audit import build_chart_data, read_mode_audit_rows
from growatt_guard.pvoutput import read_pvoutput_state
from growatt_guard.growatt_api import (
    extract_soc,
    extract_spf_output_source,
    load_context,
)
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
from growatt_guard.schedule import (
    cron_matches,
    next_scheduled_runs,
    schedule_job_id,
    schedule_job_tokens,
    today_schedule_override,
    validate_schedule,
    validate_schedule_overrides,
)
from growatt_guard.weather import choose_preserve_threshold


BASE_DIR = Path(__file__).resolve().parents[1]
DASHBOARD_FILE = BASE_DIR / "dashboard.html"
MIN_DASHBOARD_REFRESH_MINUTES = 5


def app_module() -> Any:
    module = sys.modules.get("growatt_power_guard")
    if module is not None and hasattr(module, "GrowattGuardError"):
        return module

    main_module = sys.modules.get("__main__")
    if main_module is not None and hasattr(main_module, "GrowattGuardError"):
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


def _today_job_rows(
    schedule: dict[str, Any],
    today_override: dict[str, Any],
    today: dt.date,
) -> list[tuple[str, str, str, str]]:
    skip_all = bool(today_override.get("skip_all", False))
    skip_ids = set(today_override.get("skip", []))
    replace_map = today_override.get("replace") or {}
    start = dt.datetime.combine(today, dt.time(0, 0))
    end = start + dt.timedelta(days=1)
    rows: list[tuple[str, str, str, str]] = []
    for index, job in enumerate(schedule.get("jobs", []), start=1):
        job_id = schedule_job_id(job, index)
        cron_expr = str(job.get("cron", ""))
        fires: list[dt.datetime] = []
        cursor = start
        while cursor < end:
            if cron_matches(cron_expr, cursor):
                fires.append(cursor)
            cursor += dt.timedelta(minutes=1)
        if not fires:
            continue
        cmd = " ".join(schedule_job_tokens(job, index))
        # Show interval label for sub-hourly repeating jobs
        parts = cron_expr.strip().split()
        if len(parts) == 5 and parts[0].startswith("*/") and parts[1] == "*":
            try:
                interval = int(parts[0][2:])
                time_str = f"every {interval} min"
            except ValueError:
                time_str = fires[0].strftime("%H:%M")
        else:
            time_str = fires[0].strftime("%H:%M")

        if skip_all or job_id in skip_ids:
            status_str = "SKIP"
        elif job_id in replace_map:
            repl_cmd = " ".join(schedule_job_tokens(replace_map[job_id], 0))
            status_str = f"→ {repl_cmd}"
        else:
            status_str = "OK"
        rows.append((time_str, job_id, cmd, status_str))
    return rows


def _upcoming_override_rows(overrides: dict[str, Any], today: dt.date, days: int = 14) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    cutoff = (today + dt.timedelta(days=days)).isoformat()
    today_iso = today.isoformat()
    for date_str in sorted(overrides.get("dates", {})):
        if date_str <= today_iso or date_str > cutoff:
            continue
        override = overrides["dates"][date_str]
        note = str(override.get("note", "")).strip()
        if override.get("skip_all"):
            action = "skip-all"
        else:
            parts: list[str] = []
            skip_ids = override.get("skip", [])
            if skip_ids:
                parts.append(f"skip: {', '.join(skip_ids)}")
            replace_map = override.get("replace") or {}
            if replace_map:
                parts.append(f"replace: {', '.join(replace_map)}")
            action = "; ".join(parts) if parts else "none"
        rows.append((date_str, note, action))
    return rows


def _pvoutput_card_html(state: dict[str, Any] | None, now: dt.datetime) -> str:
    if state is None:
        return (
            '<div class="card"><div class="label">PVOutput</div>'
            '<div class="value muted" style="font-size:16px">—</div>'
            '<div class="muted small">no uploads recorded</div></div>'
        )
    try:
        uploaded_at = dt.datetime.fromisoformat(str(state.get("uploaded_at", "")))
        age_seconds = max(0.0, (now - uploaded_at).total_seconds())
        time_str = uploaded_at.strftime("%H:%M")
        stale = age_seconds > 20 * 60
    except (ValueError, TypeError):
        return (
            '<div class="card"><div class="label">PVOutput</div>'
            '<div class="value muted" style="font-size:16px">—</div>'
            '<div class="muted small">invalid state</div></div>'
        )
    fields = state.get("fields", {})
    parts: list[str] = []
    v1 = fields.get("v1")
    v2 = fields.get("v2")
    if v1 is not None:
        parts.append(f"{int(v1) / 1000:.1f} kWh")
    if v2 is not None:
        parts.append(f"{v2} W PV")
    age_text = format_duration(age_seconds)
    detail = (", ".join(parts) + f" · {age_text} ago") if parts else f"{age_text} ago"
    badge_cls = "badge-warn" if stale else "badge-ok"
    badge_txt = "STALE" if stale else "OK"
    return (
        '<div class="card"><div class="label">PVOutput</div>'
        f'<div class="value"><span class="badge {badge_cls}">{badge_txt}</span>'
        f' <span style="font-size:16px">{esc(time_str)}</span></div>'
        f'<div class="muted small">{esc(detail)}</div></div>'
    )


def build_dashboard_html(
    status: dict[str, Any],
    schedule: dict[str, Any],
    overrides: dict[str, Any],
    threshold_decision: Any,
    stale_after_minutes: float = 30,
) -> str:
    now = dt.datetime.now()
    generated_at = now.astimezone()
    generated_at_iso = generated_at.isoformat(timespec="seconds")
    soc_result = extract_soc(status)
    soc = f"{soc_result[0]:g}%" if soc_result else "Not found"
    output_source = extract_spf_output_source(status)
    mode = f"{output_source[1]} [{output_source[0]}]" if output_source else "Not found"
    pause_state = read_pause_state()
    pause = pause_message(pause_state) if pause_state else "active"
    alert_state = read_battery_alert_state()
    alert = "active" if alert_state and alert_state.get("active") else "clear"
    cloud_state = read_growatt_cloud_failure_state()
    cloud_streak = int(cloud_state.get("count", 0)) if cloud_state else 0
    today_override = today_schedule_override(overrides, now.date())
    override_note = str(today_override.get("note", "")).strip() or "none"
    skipped = ", ".join(today_override.get("skip", [])) if isinstance(today_override.get("skip", []), list) else ""
    last_actions = read_mode_audit_rows(limit=8, newest_first=True)
    next_runs = next_scheduled_runs(schedule, now=now, limit=8)
    stale_minutes_text = f"{stale_after_minutes:g}"

    today_jobs = _today_job_rows(schedule, today_override, now.date())
    upcoming_overrides = _upcoming_override_rows(overrides, now.date())
    chart_data_json = json.dumps(build_chart_data(now=now))
    pvoutput_card = _pvoutput_card_html(read_pvoutput_state(), now)

    next_rows = "\n".join(
        "<tr>"
        f"<td>{esc(run_at.strftime('%Y-%m-%d %H:%M'))}</td>"
        f"<td>{esc(job.get('id', ''))}</td>"
        f"<td>{esc(job.get('name', ''))}</td>"
        f"<td>{esc(' '.join(schedule_job_tokens(job)))}</td>"
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
    today_job_rows_html = "\n".join(
        "<tr>"
        f"<td>{esc(t)}</td>"
        f"<td>{esc(jid)}</td>"
        f"<td>{esc(cmd)}</td>"
        f'<td class="status-{"skip" if st == "SKIP" else ("replace" if st.startswith("→") else "ok")}">{esc(st)}</td>'
        "</tr>"
        for t, jid, cmd, st in today_jobs
    )
    upcoming_override_rows_html = "\n".join(
        "<tr>"
        f"<td>{esc(d)}</td>"
        f"<td>{esc(n) if n else '<span class=\"muted\">—</span>'}</td>"
        f"<td>{esc(a)}</td>"
        "</tr>"
        for d, n, a in upcoming_overrides
    )

    skip_all_banner = (
        '<div class="banner-warn">⚠ All automation jobs are skipped today'
        + (f" — {esc(override_note)}" if override_note != "none" else "")
        + "</div>"
        if today_override.get("skip_all")
        else ""
    )
    upcoming_override_section = (
        f"<h2>Upcoming Overrides</h2>"
        f'<table><thead><tr><th>Date</th><th>Note</th><th>Actions</th></tr></thead><tbody>{upcoming_override_rows_html}</tbody></table>'
        if upcoming_overrides
        else ""
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
    .banner-warn {{ background: #fff2cc; color: #775800; border-radius: 8px; padding: 10px 16px; margin: 20px 0 0; font-weight: 600; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dce3e8; }}
    th, td {{ padding: 10px; border-bottom: 1px solid #e8eef2; text-align: left; font-size: 14px; }}
    th {{ background: #eef3f5; color: #34444f; }}
    tr:last-child td {{ border-bottom: 0; }}
    .status-ok {{ color: #155f34; font-weight: 600; }}
    .status-skip {{ color: #9a3526; font-weight: 600; }}
    .status-replace {{ color: #775800; font-weight: 600; }}
  </style>
</head>
<body>
  <main>
    <h1>Growatt Dashboard</h1>
    <div class="muted">Generated {esc(generated_at.strftime('%Y-%m-%d %H:%M:%S %Z'))}</div>
    {skip_all_banner}
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
      {pvoutput_card}
    </section>
    <h2>Today&#8217;s Schedule — {esc(now.strftime('%A, %Y-%m-%d'))}</h2>
    <table><thead><tr><th>Time</th><th>Job ID</th><th>Command</th><th>Status</th></tr></thead><tbody>{today_job_rows_html}</tbody></table>
    {upcoming_override_section}
    <h2>7-Day History</h2>
    <div class="card" style="padding:16px 20px;">
      <canvas id="history-chart" style="width:100%;height:160px;display:block;"></canvas>
    </div>
    <script id="chart-data" type="application/json">{chart_data_json}</script>
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
      const canvas = document.getElementById("history-chart");
      const dataEl = document.getElementById("chart-data");
      if (canvas && dataEl) {{
        try {{
          const data = JSON.parse(dataEl.textContent);
          const ctx = canvas.getContext("2d");
          const dpr = window.devicePixelRatio || 1;
          const rect = canvas.getBoundingClientRect();
          canvas.width = rect.width * dpr || 600 * dpr;
          canvas.height = 160 * dpr;
          ctx.scale(dpr, dpr);
          const W = canvas.width / dpr, H = 160;
          const PAD = {{ top: 12, right: 12, bottom: 28, left: 32 }};
          const chartW = W - PAD.left - PAD.right;
          const chartH = H - PAD.top - PAD.bottom;
          const n = data.labels.length;
          const maxVal = Math.max(1, ...data.preserve_checks, ...data.utility_switches, ...data.watchdog_repairs);
          const yStep = Math.ceil(maxVal / 4);
          ctx.font = "11px system-ui, sans-serif";
          ctx.fillStyle = "#64727d";
          for (let y = 0; y <= maxVal; y += yStep) {{
            const px = PAD.top + chartH - (y / maxVal) * chartH;
            ctx.fillText(y, 0, px + 4);
            ctx.strokeStyle = "#e8eef2"; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(PAD.left, px); ctx.lineTo(PAD.left + chartW, px); ctx.stroke();
          }}
          const groupW = chartW / n;
          const barW = Math.max(4, groupW / 4 - 2);
          const COLORS = ["#3b82f6", "#f59e0b", "#ef4444"];
          const SERIES = ["preserve_checks", "utility_switches", "watchdog_repairs"];
          SERIES.forEach(function (key, si) {{
            ctx.fillStyle = COLORS[si];
            data[key].forEach(function (val, i) {{
              const x = PAD.left + i * groupW + si * (barW + 2) + (groupW - SERIES.length * (barW + 2)) / 2;
              const barH = (val / maxVal) * chartH;
              ctx.fillRect(x, PAD.top + chartH - barH, barW, barH || 1);
            }});
          }});
          data.labels.forEach(function (label, i) {{
            ctx.fillStyle = "#64727d";
            const x = PAD.left + i * groupW + groupW / 2;
            ctx.textAlign = "center";
            ctx.fillText(label, x, H - 6);
          }});
          ctx.textAlign = "left";
          const legendY = PAD.top; const legendX = PAD.left + chartW - 200;
          [["Preserve checks", "#3b82f6"], ["Utility switches", "#f59e0b"], ["Watchdog repairs", "#ef4444"]].forEach(function (item, i) {{
            ctx.fillStyle = item[1];
            ctx.fillRect(legendX + i * 70, legendY, 8, 8);
            ctx.fillStyle = "#64727d";
            ctx.fillText(item[0].split(" ")[0], legendX + i * 70 + 11, legendY + 8);
          }});
        }} catch (e) {{ /* chart render failed */ }}
      }}
    }})();
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
    _, _, status = load_context(config)
    schedule = validate_schedule()
    overrides = validate_schedule_overrides(schedule)
    threshold_decision = choose_preserve_threshold(config)
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
