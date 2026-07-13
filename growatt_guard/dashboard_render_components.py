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
    def number(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None

    def format_kwh(value: Any) -> str:
        numeric = number(value)
        return "--" if numeric is None else f"{numeric:.1f} kWh"

    def width(value: Any) -> str:
        numeric = number(value)
        return f"{max(0.0, min(100.0, numeric or 0.0)):.0f}%"

    daily = view.get("daily_history") or {}
    labels = list(daily.get("labels") or [])[-7:]
    charged = list(daily.get("charge_kwh") or [])[-7:]
    discharged = list(daily.get("discharge_kwh") or [])[-7:]
    pv_days = list(daily.get("pv_kwh") or [])[-7:]
    while len(labels) < 7:
        labels.insert(0, "--")
        charged.insert(0, None)
        discharged.insert(0, None)
        pv_days.insert(0, None)

    battery_max = max([number(v) or 0.0 for v in charged + discharged] + [1.0])
    battery_bars = "\n".join(
        f'<div class="design-bar-day"><div class="design-bars">'
        f'<span class="design-bar charged" style="height:{max(3.0, (number(charge) or 0.0) / battery_max * 100):.0f}%" title="Charged {format_kwh(charge)}"></span>'
        f'<span class="design-bar discharged" style="height:{max(3.0, (number(discharge) or 0.0) / battery_max * 100):.0f}%" title="Discharged {format_kwh(discharge)}"></span>'
        f'</div><span>{esc(label)}</span></div>'
        for label, charge, discharge in zip(labels, charged, discharged)
    )

    today_remaining = number(view.get("today_remaining_kwh"))
    today_pv = number(view.get("pv_today_kwh"))
    tomorrow_pv = number(view.get("tomorrow_pv_kwh"))
    outlook_values = pv_days[-6:-1] + [
        (today_pv + today_remaining) if today_pv is not None and today_remaining is not None else today_pv,
        tomorrow_pv,
    ]
    outlook_labels = labels[-6:-1] + ["TODAY", "TMRW"]
    outlook_max = max([value or 0.0 for value in outlook_values] + [1.0])
    outlook_cards = "\n".join(
        f'<div class="design-forecast-day {"forecast" if index >= 5 else "actual"}">'
        f'<span>{esc(label)}</span><b>{"☀" if (value or 0) >= outlook_max * .65 else "☁"}</b>'
        f'<strong>{"--" if value is None else f"{value:.1f}"}</strong><em>kWh</em>'
        f'<i><u style="width:{0 if value is None else value / outlook_max * 100:.0f}%"></u></i>'
        f'</div>'
        for index, (label, value) in enumerate(zip(outlook_labels, outlook_values))
    )

    recommendations = view.get("recommendations") or []
    recommendation_cards = "\n".join(
        f'<article class="design-recommendation rec-{esc(str(item.get("level", "good")))}">'
        f'<div><span>{esc(str(item.get("icon", "OK")))}</span><em>{esc(str(item.get("level", "good")))}</em></div>'
        f'<strong>{esc(str(item.get("title", "Recommendation")))}</strong>'
        f'<p>{esc(str(item.get("text", "")))}</p><small>{esc(str(item.get("meta", "")))}</small>'
        f'</article>'
        for item in recommendations[:3]
    ) or '<article class="design-recommendation rec-good"><strong>No action required</strong><p>Automation is operating normally.</p></article>'

    timeline = view.get("schedule_timeline") or []
    operation_rows = "\n".join(
        f'<div class="design-operation"><time>{esc(str(item.get("time", "--")))}</time>'
        f'<div><strong>{esc(str(item.get("name", "Automation job")))}</strong><span>{esc(str(item.get("detail", "")))}</span></div>'
        f'<em class="operation-{esc(str(item.get("state", "scheduled")))}">{esc(str(item.get("status", "QUEUED")))}</em></div>'
        for item in timeline[:5]
    ) or '<div class="design-operation"><time>--</time><div><strong>No jobs scheduled today</strong><span>The automation queue is clear.</span></div><em>CLEAR</em></div>'

    supply_pv = width(view.get("pv_supply_pct"))
    supply_grid = width(view.get("grid_supply_pct"))
    demand_load = width(view.get("load_demand_pct"))
    demand_charge = width(view.get("charge_demand_pct"))
    battery_charge = width(view.get("charge_battery_pct"))
    battery_discharge = width(view.get("discharge_battery_pct"))
    solar_now_level = "Night" if (view.get("pv_w") or 0) < 20 else "Active"
    return f"""
    <section class="night-console design-dashboard" aria-label="Solar home dashboard">
      <header class="design-header">
        <div>
          <p>{esc(view['greeting'])} · {esc(view['generated_time'])}</p>
          <h1>{esc(view['headline'])}</h1>
          <span>{esc(view['generated_date'])} · {esc(view['mode'])} · {esc(view['weather_detail'])}</span>
        </div>
        <div class="design-header-actions">
          {_inline_badge('Tonight: ' + str(view['tonight_title']), view['night_topup_class'])}
          {_inline_badge('Data ' + str(view['quality_display']), view['quality_badge_class'])}
          {_inline_badge(str(view['health_display']), view['health_badge_class'])}
          <button class="theme-toggle" type="button" onclick="toggleDashLayout()">Operations</button>
        </div>
      </header>

      <div class="design-grid design-primary-grid">
        <article class="design-card design-battery-card">
          <div class="design-card-head"><span>Battery Reserve</span>{_inline_badge(view['soc_health'], view['soc_health_class'])}</div>
          <div class="design-battery-main">
            <div class="design-soc-ring" style="--soc:{float(view['soc_gauge_value']):.0f}%"><div><strong>{esc(view['soc'])}</strong><span>{esc(view['battery_power_label'])}</span></div></div>
            <div class="design-metric-stack">
              <div><span>Usable reserve</span><strong>{esc(view['usable_kwh_display'])}</strong><em>above {esc(view['reserve_floor_display'])} floor</em></div>
              <div><span>Live flow</span><strong>{esc(view['battery_flow_display'])}</strong><em>{esc(view['battery_context'])}</em></div>
              <div><span>Runtime</span><strong>{esc(view['est_runtime'])}</strong><em>{esc(view['vbat'])} bus</em></div>
            </div>
          </div>
          <div class="design-stat-row"><div><strong>{esc(view['charge_today_display'])}</strong><span>charged today</span></div><div><strong>{esc(view['discharge_today_display'])}</strong><span>discharged</span></div><div><strong>{esc(view['battery_throughput_display'])}</strong><span>throughput</span></div></div>
        </article>

        <article class="design-card design-risk-card">
          <div class="design-card-head"><span>Tonight outlook</span>{_inline_badge(view['tonight_title'], view['night_topup_class'])}</div>
          <div class="design-risk-hero"><strong>{esc(view['tonight_projection_display'])}</strong><span>projected at sunrise</span><p>{esc(view['tonight_detail'])}</p></div>
          <div class="design-risk-meter"><i style="width:{width(view.get('tonight_projection_value'))}"></i><b style="left:{width(view.get('reserve_target_value'))}" title="Reserve target"></b></div>
          <div class="design-stat-row"><div><strong>{esc(view['topup_needed_display'])}</strong><span>Top-up needed</span></div><div><strong>{esc(view['expected_grid_kwh'])}</strong><span>grid top-up</span></div><div><strong>{esc(view['sunrise_display'])}</strong><span>to sunrise</span></div></div>
        </article>
      </div>

      <div class="design-grid design-primary-grid">
        <article class="design-card design-solar-card">
          <div class="design-card-head"><span>Solar Detail</span>{_inline_badge(solar_now_level, 'badge-neutral')}</div>
          <div class="design-solar-main"><strong>{esc(view['pv_power_display'])}</strong><span>right now</span><p>{esc(view['forecast_short'])}</p></div>
          <div class="design-sunline" aria-hidden="true"><i></i><b></b></div>
          <div class="design-stat-row"><div><strong>{esc(view['pv_today_display'])}</strong><span>today</span></div><div><strong>{esc(view['pv_cover_display'])}</strong><span>load covered</span></div><div><strong>{esc(view['tomorrow_pv'])}</strong><span>tomorrow</span></div></div>
        </article>

        <article class="design-card design-flow-card">
          <div class="design-card-head"><span>Live power flow</span><em>{esc(view['bat_status'])} · bypass {esc(view['bypass_label']).lower()}</em></div>
          <div class="design-flow-map">
            <div class="design-flow-sources"><div class="flow-solar"><span>☀ Solar</span><strong>{esc(view['pv_power_display'])}</strong></div><div class="flow-grid"><span>⌁ Grid</span><strong>{esc(view['grid_power_display'])}</strong></div></div>
            <div class="design-flow-core"><i></i><div><span>Inverter</span><strong>{esc(view['mode'])}</strong></div><i></i></div>
            <div class="design-flow-sources"><div class="flow-battery"><span>▮ Battery</span><strong>{esc(view['battery_flow_display'])}</strong></div><div class="flow-load"><span>⌂ Home</span><strong>{esc(view['load_power_display'])}</strong></div></div>
          </div>
          <div class="design-stat-row"><div><strong>{esc(view['grid_today_display'])}</strong><span>grid today</span></div><div><strong>{esc(view['load_today_display'])}</strong><span>load today</span></div><div><strong>{esc(view['battery_throughput_display'])}</strong><span>throughput</span></div><div><strong>{esc(view['load_pct'])}</strong><span>load level</span></div></div>
        </article>
      </div>

      <div class="design-grid design-secondary-grid">
        <article class="design-card design-chart-card">
          <div class="design-card-head"><span>7-day battery</span><em><b class="legend-charged"></b> charged <b class="legend-discharged"></b> discharged</em></div>
          <div class="design-bar-chart">{battery_bars}</div>
        </article>
        <article class="design-card design-mix-card">
          <div class="design-card-head"><span>Today mix</span><em>supply vs demand</em></div>
          <div class="design-mix-lines">
            <div><p><span>Supply {esc(view['supply_total_display'])}</span><strong>{esc(view['pv_supply_display'])} solar</strong></p><i><b class="mix-pv" style="width:{supply_pv}"></b><b class="mix-grid-source" style="width:{supply_grid}"></b></i></div>
            <div><p><span>Demand {esc(view['demand_total_display'])}</span><strong>{esc(view['load_demand_display'])} house</strong></p><i><b class="mix-load" style="width:{demand_load}"></b><b class="mix-charge" style="width:{demand_charge}"></b></i></div>
            <div><p><span>Battery activity {esc(view['battery_activity_display'])}</span><strong>{esc(view['battery_net_display'])} net</strong></p><i><b class="mix-charge" style="width:{battery_charge}"></b><b class="mix-discharge" style="width:{battery_discharge}"></b></i></div>
          </div>
        </article>
      </div>

      <article class="design-card design-outlook-card">
        <div class="design-card-head"><span>7-day solar outlook</span><em>actual history · today projection · tomorrow forecast</em></div>
        <div class="design-forecast-strip">{outlook_cards}</div>
      </article>

      <section class="design-recommendations"><div class="design-section-label">Recommendations</div><div class="design-recommendation-grid">{recommendation_cards}</div></section>

      <article class="design-operations">
        <div class="design-operations-head"><span>Automation &amp; operations</span><div><strong>{len(view.get('today_jobs') or [])}</strong><small>jobs today</small><strong>{esc(view['next_action_relative'])}</strong><small>next job</small></div></div>
        <div>{operation_rows}</div>
      </article>
      <footer>GROWATT · LAST SYNC {esc(view['generated_time'])} · DATA {esc(view['quality_display'])}</footer>
    </section>"""
