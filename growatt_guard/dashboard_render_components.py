from __future__ import annotations
import datetime as dt
import html
from typing import Any

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


def _status_badge_class(level: str) -> str:
    if level in {"comfortable", "good", "ok"}:
        return "badge-ok"
    if level in {"watch", "unknown"}:
        return "badge-warn"
    return "badge-fail"

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



def _inline_badge(text: Any, class_name: str) -> str:
    return f'<span class="badge {esc(class_name)}">{esc(text)}</span>'


def _stat_block(label: Any, value: Any, detail: Any = "", class_name: str = "") -> str:
    class_attr = f' class="{esc(class_name)}"' if class_name else ""
    detail_html = f"<em>{esc(detail)}</em>" if detail else ""
    return (
        f"<div{class_attr}>"
        f"<span>{esc(label)}</span>"
        f"<strong>{esc(value)}</strong>"
        f"{detail_html}"
        "</div>"
    )


def _glance_card(
    class_name: str,
    title: Any,
    value: Any,
    badge_text: Any,
    badge_class: str,
    rows: list[tuple[Any, Any]],
    detail: Any = "",
) -> str:
    row_html = "".join(
        f'<div class="{"glance-primary-stat" if idx == 0 else ""}"><span>{esc(label)}</span><strong>{esc(row_value)}</strong></div>'
        for idx, (label, row_value) in enumerate(rows)
    )
    detail_html = f'<p class="glance-detail">{esc(detail)}</p>' if detail else ""
    return (
        f'<article class="glance-card {esc(class_name)}">'
        '<div class="glance-head">'
        f'<div><div class="label">{esc(title)}</div><div class="glance-value">{esc(value)}</div></div>'
        f'{_inline_badge(badge_text, badge_class)}'
        '</div>'
        f'{detail_html}'
        f'<div class="glance-stats">{row_html}</div>'
        '</article>'
    )



def _render_insight_cards(items: Any) -> str:
    return "\n".join(
        (
            '<article class="card insight-card">'
            f'<div class="label">{esc(str(item.get("label", "")))}</div>'
            f'<div class="value">{_inline_badge(str(item.get("title", "Unknown")), _status_badge_class(str(item.get("level", "unknown"))))}</div>'
            f'<div class="muted small">{esc(str(item.get("detail", "")))}</div>'
            "</article>"
        )
        for item in items
        if isinstance(item, dict)
    )


def _metric_card(label: Any, value: Any, detail: Any, accent: str = "", meter_width: float | None = None) -> str:
    accent_class = f" accent-{accent}" if accent else ""
    meter_class = f" {accent}-meter" if accent else ""
    meter_html = ""
    if meter_width is not None:
        width = max(0.0, min(100.0, float(meter_width)))
        meter_html = f'<div class="metric-meter{meter_class}"><span style="width:{width:.0f}%"></span></div>'
    return (
        f'<article class="card metric-card{accent_class}">'
        '<div class="metric-head"><div>'
        f'<div class="label">{esc(label)}</div><div class="value">{esc(value)}</div>'
        '</div></div>'
        f'{meter_html}'
        f'<div class="muted small">{esc(detail)}</div>'
        '</article>'
    )


def _render_status_rows(rows: list[tuple[Any, Any, str]]) -> str:
    return "\n".join(
        (
            '<div class="status-row">'
            f'<span>{esc(label)}</span>'
            f'{_inline_badge(value, badge_class)}'
            "</div>"
        )
        for label, value, badge_class in rows
    )


def _render_activity_items(rows: list[dict[str, Any]]) -> str:
    items = "\n".join(
        (
            '<li class="activity-item">'
            '<div>'
            f'<strong>{esc(row.get("action", "") or row.get("command", "") or "mode decision")}</strong>'
            f'<span>{esc(row.get("timestamp", ""))}</span>'
            '</div>'
            f'<span class="summary-meta">SOC {esc(row.get("soc", "") or "--")}</span>'
            '</li>'
        )
        for row in rows[:5]
    )
    return items or '<li class="activity-item muted">No recent mode decisions recorded.</li>'


def _render_timeline_items(items: list[dict[str, Any]]) -> str:
    timeline_badges = {
        "next": "badge-ok",
        "monitoring": "badge-ok",
        "upcoming": "badge-warn",
        "passed": "badge-warn",
        "skipped": "badge-warn",
        "replaced": "badge-warn",
    }
    rendered = "\n".join(
        (
            f'<li class="timeline-item timeline-{esc(str(item.get("state", "unknown")))}">'
            '<div class="timeline-marker" aria-hidden="true"></div>'
            '<div class="timeline-main">'
            f'<strong>{esc(str(item.get("time", "--")))} - {esc(str(item.get("name", "")))}</strong>'
            f'<span>{esc(str(item.get("detail", "")))}</span>'
            '</div>'
            f'{_inline_badge(str(item.get("status", "Unknown")), timeline_badges.get(str(item.get("state", "")), "badge-warn"))}'
            '</li>'
        )
        for item in items[:8]
    )
    return rendered or '<li class="timeline-item muted">No automation jobs scheduled today.</li>'



def _render_energy_outlook(view: dict[str, Any]) -> str:
    cards = "\n".join(
        '<div class="card">'
        f'<div class="label">{esc(label)}</div>'
        f'<div class="value">{esc(value)}</div>'
        f'<div class="muted small">{esc(detail)}</div>'
        '</div>'
        for label, value, detail in view["cards"]
    )
    return f"""
    <div class="section-head" id="forecast">
      <div>
        <h2>Energy Outlook</h2>
        <div class="muted">Predictive view of generation, reserve, grid use, and weather impact.</div>
      </div>
      {_inline_badge('Confidence: ' + str(view['confidence']), 'badge-neutral')}
    </div>
    <section class="grid ops-grid" aria-label="Energy outlook">
      {cards}
    </section>"""


def _render_daily_mix(view: dict[str, Any]) -> str:
    def _bar(primary_width: str, neutral_width: str, label: str) -> str:
        return (
            f'<div class="mix-bar" aria-label="{esc(label)}">'
            f'<span class="mix-segment primary" style="width:{esc(primary_width)}%"></span>'
            f'<span class="mix-segment neutral" style="width:{esc(neutral_width)}%"></span>'
            '</div>'
        )

    def _panel(title: str, total: str, bar_html: str, rows: list[tuple[str, str]]) -> str:
        legend = "".join(f'<div><span>{esc(label)}</span><strong>{esc(value)}</strong></div>' for label, value in rows)
        return f"""
        <div class="mix-panel">
          <div class="mix-row-head"><strong>{esc(title)}</strong><span>{esc(total)}</span></div>
          {bar_html}
          <div class="mix-legend">{legend}</div>
        </div>"""

    panels = "\n".join(
        [
            _panel(
                "Supply",
                view["supply_total_display"],
                _bar(view["pv_supply_width"], view["grid_supply_width"], "PV and grid supply mix"),
                [("PV", view["pv_supply_label"]), ("Grid", view["grid_supply_label"])],
            ),
            _panel(
                "Demand",
                view["demand_total_display"],
                _bar(view["load_demand_width"], view["charge_demand_width"], "Load and battery charging demand mix"),
                [("House load", view["load_demand_label"]), ("Stored", view["charge_demand_label"])],
            ),
            _panel(
                "Battery",
                view["battery_activity_display"],
                _bar(view["charge_battery_width"], view["discharge_battery_width"], "Battery charge and discharge mix"),
                [(view["battery_net_title"], view["battery_net_display"]), ("Discharged", view["discharge_battery_label"])],
            ),
        ]
    )
    return f"""
    <section class="daily-mix card" aria-label="Today energy mix">
      <div class="mix-header">
        <div>
          <div class="label">Today Mix</div>
          <div class="muted small">Where energy came from, where it went, and the battery net position.</div>
        </div>
        {_inline_badge('Reported counters', view['quality_badge_class'])}
      </div>
      <div class="mix-grid">
        {panels}
      </div>
    </section>
"""


def _render_night_view(view: dict[str, Any]) -> str:
    solar_now_level = "night" if (view.get("pv_w") or 0) < 20 else "active"
    solar_detail = (
        "PV is offline now; tomorrow forecast remains the next solar signal."
        if solar_now_level == "night"
        else f"PV is covering {view['pv_cover_display']} of live house load."
    )
    day_total_items = "\n".join(
        _stat_block(label, value, detail, "night-total-item")
        for label, value, detail in [
            ("PV Today", view["pv_today_display"], f"Lifetime {view['pv_lifetime']}"),
            ("Tomorrow PV", view["tomorrow_pv"], view["forecast_short"]),
            ("Grid Today", view["grid_today_display"], view["grid_status_text"]),
            ("Battery Charge", view["charge_today_display"], "stored today"),
            ("Battery Discharge", view["discharge_today_display"], "used today"),
            ("Battery Throughput", view["battery_throughput_display"], "charge + discharge"),
        ]
    )
    battery_stats = "\n".join(
        [
            _stat_block("Current power", view["battery_flow_display"], view["battery_context"]),
            _stat_block("Usable reserve", view["usable_kwh_display"], f"Floor {view['reserve_floor_display']}"),
            _stat_block("Voltage", view["vbat"], "Battery bus reading"),
        ]
    )
    battery_subgrid = "\n".join(
        [
            _stat_block("Charge today", view["charge_today_display"]),
            _stat_block("Discharge today", view["discharge_today_display"]),
            _stat_block("Throughput", view["battery_throughput_display"]),
            _stat_block("Runtime", view["est_runtime"]),
        ]
    )
    solar_stats = "\n".join(
        [
            _stat_block("PV Today", view["pv_today_display"], solar_detail, "night-primary-stat"),
            _stat_block("PV Lifetime", view["pv_lifetime"], "Total Growatt production"),
            _stat_block("Tomorrow PV", view["tomorrow_pv"], view["forecast_short"]),
            _stat_block("Weather", view["weather_short"], view["weather_detail"]),
        ]
    )
    risk_scores = "\n".join(
        [
            _stat_block("Projected sunrise", view["tonight_projection_display"]),
            _stat_block("Reserve target", view["reserve_target_display"]),
            _stat_block("Top-up needed", view["topup_needed_display"]),
        ]
    )
    return f"""
    <section class="night-console" aria-label="Night operations solar and battery view">
      <div class="night-context-strip">
        {_inline_badge('Data: ' + str(view['quality_display']), view['quality_badge_class'])}
        {_inline_badge('Next: ' + str(view['next_action_relative']) + ' - ' + str(view['next_action_title']), 'badge-neutral')}
      </div>
      <div class="night-hero-grid">
        <article class="night-panel night-battery">
          <div class="night-panel-head">
            <div><div class="label">Battery Reserve</div><div class="night-panel-title">{esc(view['soc'])}</div></div>
            {_inline_badge(view['soc_health'], view['soc_health_class'])}
          </div>
          <div class="night-battery-main">
            <div class="soc-ring night-soc-ring" style="--soc:{float(view['soc_gauge_value']):.0f}%">
              <div class="soc-core"><strong>{esc(view['soc'])}</strong><span>{esc(view['battery_power_label'])}</span></div>
            </div>
            <div class="night-metric-stack">{battery_stats}</div>
          </div>
          <div class="night-subgrid">{battery_subgrid}</div>
        </article>
        <article class="night-panel night-solar">
          <div class="night-panel-head">
            <div><div class="label">Solar Detail</div><div class="night-panel-title">{esc(view['pv_power_display'])}</div></div>
            {_inline_badge(solar_now_level.capitalize(), 'badge-neutral')}
          </div>
          <div class="night-solar-grid">{solar_stats}</div>
          <div class="night-spark" aria-hidden="true">
            <span style="height:18%"></span><span style="height:32%"></span><span style="height:48%"></span>
            <span style="height:72%"></span><span style="height:88%"></span><span style="height:64%"></span>
            <span style="height:42%"></span><span style="height:12%"></span>
          </div>
        </article>
        <article class="night-panel night-risk">
          <div class="night-panel-head">
            <div><div class="label">Tonight Risk</div><div class="night-panel-title">{_inline_badge(view['tonight_title'], view['night_topup_class'])}</div></div>
            {_inline_badge(view['next_action_relative'], 'badge-neutral')}
          </div>
          <div class="night-risk-score">{risk_scores}</div>
          <div class="night-risk-note">{esc(view['tonight_detail'])}</div>
          <div class="night-next">
            <span>Next automation</span><strong>{esc(view['next_action_title'])}</strong><em>{esc(view['next_action_detail'])}</em>
          </div>
        </article>
      </div>
      <section class="night-flow" aria-label="Night live power flow">
        <div class="night-flow-node solar"><span>Solar Now</span><strong>{esc(view['pv_power_display'])}</strong><em>{esc(view['pv_today_display'])} today</em></div>
        <div class="night-flow-arrow">-&gt;</div>
        <div class="night-flow-node inverter"><span>Inverter</span><strong>{esc(view['mode'])}</strong><em>{esc(view['bat_status'])}</em></div>
        <div class="night-flow-arrow">-&gt;</div>
        <div class="night-flow-node load"><span>Load Now</span><strong>{esc(view['load_power_display'])}</strong><em>{esc(view['load_today_display'])} today</em></div>
        <div class="night-flow-node battery"><span>Battery</span><strong>{esc(view['soc'])}</strong><em>{esc(view['battery_context'])} - {esc(view['battery_flow_display'])}</em></div>
        <div class="night-flow-node grid-source"><span>Grid Now</span><strong>{esc(view['grid_power_display'])}</strong><em>{esc(view['grid_now_detail'])}</em></div>
      </section>
      <section class="night-totals" aria-label="Solar and battery day totals">{day_total_items}</section>
    </section>"""


