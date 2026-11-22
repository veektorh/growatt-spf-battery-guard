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


def _pvoutput_status(state: dict[str, Any] | None, now: dt.datetime) -> tuple[str, str, str]:
    """Return (badge text, badge class, detail) for the PVOutput health row."""
    if state is None:
        return ("None", "badge-neutral", "no uploads recorded")
    try:
        uploaded_at = dt.datetime.fromisoformat(str(state.get("uploaded_at", "")))
        age_seconds = max(0.0, (now - uploaded_at).total_seconds())
        time_str = uploaded_at.strftime("%H:%M")
        stale = age_seconds > 20 * 60
    except (ValueError, TypeError):
        return ("Unknown", "badge-warn", "invalid state")
    fields = state.get("fields", {})
    parts: list[str] = []
    v1 = fields.get("v1")
    v2 = fields.get("v2")
    if v1 is not None:
        parts.append(f"{int(v1) / 1000:.1f} kWh")
    if v2 is not None:
        parts.append(f"{v2} W PV")
    age_text = format_duration(age_seconds)
    detail = f"Last upload {time_str} · " + (", ".join(parts) + f" · {age_text} ago" if parts else f"{age_text} ago")
    return ("STALE" if stale else "OK", "badge-warn" if stale else "badge-ok", detail)


def _inline_badge(text: Any, class_name: str) -> str:
    return f'<span class="badge {esc(class_name)}">{esc(text)}</span>'


def _kv_rows(rows: list[tuple[Any, Any]]) -> str:
    """Label/value pairs for a ``<dl class="kv">``."""
    return "\n".join(f"<dt>{esc(label)}</dt><dd>{esc(value)}</dd>" for label, value in rows)


def _live_cell(accent: str, label: Any, value: Any, detail: Any) -> str:
    """One cell of the live power band on the Now tab."""
    return (
        f'<div class="live-cell live-{esc(accent)}">'
        f'<h3><i aria-hidden="true"></i>{esc(label)}</h3>'
        f"<b>{esc(value)}</b>"
        f"<span>{esc(detail)}</span>"
        "</div>"
    )


def _meter(fill_pct: float | None, accent: str, marker_pct: float | None = None, marker_label: str = "") -> str:
    """A thin horizontal meter with an optional labelled marker."""
    def clamp(value: float) -> float:
        return max(0.0, min(100.0, value))

    fill = f'<u class="meter-{esc(accent)}" style="width:{clamp(fill_pct):.1f}%"></u>' if fill_pct is not None else ""
    marker = ""
    if marker_pct is not None:
        position = clamp(marker_pct)
        edge = " edge-r" if position > 70 else (" edge-l" if position < 12 else "")
        marker = f'<s class="meter-mark{edge}" style="left:{position:.1f}%"><em>{esc(marker_label)}</em></s>'
    return f'<div class="meter">{fill}{marker}</div>'


def _tile(
    tile_id: str,
    label: Any,
    hero: Any,
    hero_detail: Any,
    badge_text: Any,
    badge_class: str,
    meter_html: str,
    rows: list[tuple[Any, Any]],
    note: Any = "",
) -> str:
    """One of the three answer tiles at the top of the Now tab."""
    note_html = f'<p class="muted small">{esc(note)}</p>' if note else ""
    return (
        f'<article class="panel tile" id="{esc(tile_id)}">'
        f'<div class="eyebrow"><h2>{esc(label)}</h2>{_inline_badge(badge_text, badge_class)}</div>'
        f'<div class="hero"><b>{esc(hero)}</b><span>{esc(hero_detail)}</span></div>'
        f"{meter_html}"
        f'<dl class="kv">{_kv_rows(rows)}</dl>'
        f"{note_html}"
        "</article>"
    )


def _render_alerts(alerts: list[tuple[str, str, str]]) -> str:
    """Attention rows for the Now tab: (badge class, title, detail)."""
    return "\n".join(
        (
            f'<div class="alert alert-{"fail" if badge_class == "badge-fail" else "warn"}">'
            f"{_inline_badge(title, badge_class)}"
            f"<span>{esc(detail)}</span>"
            "</div>"
        )
        for badge_class, title, detail in alerts
    )


def _render_balance_bar(title: str, parts: list[tuple[str, str, float | None]]) -> str:
    """A stacked horizontal bar with a legend: parts are (accent, label, kWh)."""
    known = [(accent, label, float(value)) for accent, label, value in parts if isinstance(value, (int, float))]
    total = sum(value for _, _, value in known)
    total_text = f"{total:.1f} kWh" if known else "--"
    segments = "".join(
        f'<i class="seg-{esc(accent)}" style="flex:{max(value, 0.01):.2f}"></i>' for accent, _, value in known
    )
    legend = "".join(
        (
            f'<span><i class="seg-{esc(accent)}" aria-hidden="true"></i>{esc(label)} '
            f"<b>{value:.1f} kWh{f' · {value / total * 100:.0f}%' if total > 0 else ''}</b></span>"
        )
        for accent, label, value in known
    )
    return (
        f'<div class="balance"><div class="balance-head"><span>{esc(title)}</span><span>{esc(total_text)}</span></div>'
        f'<div class="hbar">{segments}</div>'
        f'<div class="legend">{legend}</div></div>'
    )


def _render_insight_rows(items: Any) -> str:
    """Today-against-usual rows from the daily insight items."""
    def fmt(item: dict[str, Any], key: str) -> str:
        value = item.get(key)
        if not isinstance(value, (int, float)):
            return "--"
        return f"{value:.0f}%" if item.get("key") == "soc" else f"{value:.1f} kWh"

    return "\n".join(
        (
            "<tr>"
            f'<td>{esc(str(item.get("label", "")))}<small>{esc(str(item.get("detail", "")))}</small></td>'
            f'<td class="r">{esc(fmt(item, "current"))}</td>'
            f'<td class="r">{esc(fmt(item, "baseline"))}</td>'
            f'<td class="r">{_inline_badge(str(item.get("title", "Unknown")), _status_badge_class(str(item.get("level", "unknown"))))}</td>'
            "</tr>"
        )
        for item in items
        if isinstance(item, dict)
    )


def _render_recommendations(items: list[dict[str, Any]]) -> str:
    labels = {"high": "Act", "watch": "Watch", "good": "Fine"}
    return "\n".join(
        (
            '<article class="rec">'
            f'{_inline_badge(labels.get(str(r.get("level", "good")), "Note"), _status_badge_class(str(r.get("level", "good"))))}'
            f'<strong>{esc(str(r.get("title", "Recommendation")))}</strong>'
            f'<p>{esc(str(r.get("text", "")))} <em>{esc(str(r.get("meta", "")))}</em></p>'
            "</article>"
        )
        for r in items
    )


_TIMELINE_BADGES = {
    "next": "badge-ok",
    "monitoring": "badge-ok",
    "upcoming": "badge-neutral",
    "passed": "badge-neutral",
    "skipped": "badge-warn",
    "replaced": "badge-warn",
}


def _render_timeline_rows(items: list[dict[str, Any]]) -> str:
    """Schedule timeline entries as table rows; used for timed jobs and monitors."""
    return "\n".join(
        (
            f'<tr class="timeline-{esc(str(item.get("state", "unknown")))}">'
            f'<td class="t">{esc(str(item.get("time", "--")))}</td>'
            f'<td>{esc(str(item.get("name", "")))}<small>{esc(str(item.get("detail", "")))}</small></td>'
            f'<td class="r">{_inline_badge(str(item.get("status", "Unknown")), _TIMELINE_BADGES.get(str(item.get("state", "")), "badge-warn"))}</td>'
            "</tr>"
        )
        for item in items
    )


def _render_health_rows(rows: list[tuple[Any, Any, Any, str]]) -> str:
    """System health rows: (label, detail, badge text, badge class)."""
    return "\n".join(
        (
            "<tr>"
            f'<td class="health-label">{esc(label)}</td>'
            f"<td>{esc(detail)}</td>"
            f'<td class="r">{_inline_badge(badge_text, badge_class)}</td>'
            "</tr>"
        )
        for label, detail, badge_text, badge_class in rows
    )


def _clock_hhmm(value: Any) -> str:
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else "?"


def _clock_range(after: Any, by: Any) -> str:
    """An edge bracketed by two readings: the last on one side, the first on the other."""
    if not after:
        return f"by {_clock_hhmm(by)}"
    return f"{_clock_hhmm(after)}–{_clock_hhmm(by)}"


def _supply_strip_text(current: dict[str, Any] | None) -> str:
    """Short estate-supply state for the Now tab strip."""
    if not current:
        return "no reading yet"
    if current.get("on"):
        return "On"
    if current.get("off_after") and current.get("off_by"):
        return f"Off since {_clock_range(current.get('off_after'), current.get('off_by'))}"
    if current.get("off_by"):
        # No on-reading before this cut in the history: only its first off reading is known.
        return f"Off, seen at {_clock_hhmm(current.get('off_by'))}"
    return "Off"


def _render_supply_rows(events: list[dict[str, Any]], limit: int = 14) -> str:
    """Observed-supply table rows, newest first."""
    rows: list[str] = []
    for event in list(reversed(events))[:limit]:
        kind = str(event.get("kind", ""))
        scheduled = str(event.get("scheduled") or "")
        scheduled_html = esc(scheduled) if scheduled else '<span class="badge badge-warn">unscheduled</span>'
        if kind in ("no-cut", "no-data"):
            detail = "supply stayed on" if kind == "no-cut" else "no readings"
            rows.append(
                "<tr>"
                f'<td class="t">{esc(event.get("date", ""))}</td>'
                f"<td>{scheduled_html}</td>"
                f'<td colspan="3" class="muted">{esc(detail)}</td>'
                "</tr>"
            )
            continue
        back = "still off" if event.get("ongoing") else _clock_range(event.get("end_after"), event.get("end_by"))
        soc = event.get("soc_at_start")
        soc_text = f"{soc:g}%" if isinstance(soc, (int, float)) else "—"
        rows.append(
            "<tr>"
            f'<td class="t">{esc(event.get("date", ""))}</td>'
            f"<td>{scheduled_html}</td>"
            f"<td>{esc(_clock_range(event.get('start_after'), event.get('start_by')))}</td>"
            f"<td>{esc(back)}</td>"
            f'<td class="r">{esc(soc_text)}</td>'
            "</tr>"
        )
    return "\n".join(rows)


def _short_date_text(value: Any) -> str:
    try:
        return dt.date.fromisoformat(str(value)).strftime("%d %b")
    except (TypeError, ValueError):
        return "?"


def _short_moment_text(value: Any) -> str:
    try:
        return dt.datetime.fromisoformat(str(value)).strftime("%d %b %H:%M")
    except (TypeError, ValueError):
        return "?"


def _render_utility_units_panel(report: dict[str, Any] | None) -> str:
    """Prepaid-meter balance, burn rate and consumption for the Energy tab."""
    report = report or {}
    current = report.get("current")
    if not current:
        return (
            '<article class="panel" id="utility-units">'
            '<div class="eyebrow"><h2>Utility Units</h2>'
            + _inline_badge("No readings", "badge-neutral")
            + "</div>"
            '<p class="muted small">The prepaid meter is not tracked yet. Record its balance with '
            "<code>utility-units reading &lt;units&gt;</code> and each purchase with "
            "<code>utility-units topup &lt;units&gt;</code>.</p>"
            "</article>"
        )

    cover = report.get("cover")
    badge_text = f"{cover['days']:g} days cover" if cover else "Balance only"
    badge_class = {
        "ok": "badge-ok",
        "watch": "badge-warn",
    }.get(str(report.get("level") or ""), "badge-neutral")
    rows: list[tuple[str, str]] = [
        (
            "Meter balance",
            f"{current['balance']:g} units · {_short_moment_text(current['at'])}",
        )
    ]
    burn = report.get("burn")
    rows.append(
        (
            "Burn rate",
            f"{burn['units_per_day']:g} units/day over {burn['days']:g} days"
            if burn
            else "Need a second reading",
        )
    )
    rows.append(
        (
            "Days of cover",
            f"{cover['days']:g} days · runs out {_short_date_text(cover['runs_out'])}"
            if cover
            else "Unknown",
        )
    )
    period = report.get("period") or {}
    span = f"{_short_date_text(period.get('from'))}–{_short_date_text(period.get('to'))}"
    if period.get("consumed") is not None:
        rows.append(("This period", f"{span}: {period['consumed']:g} units used"))
        rows.append(("Bought", f"{period['purchased']:g} units"))
        if period.get("inverter_units") is not None:
            days = period.get("inverter_days") or {}
            rows.append(
                (
                    "Inverter grid",
                    f"{period['inverter_units']:g} units "
                    f"({days.get('covered', 0)} of {days.get('expected', 0)} whole days)",
                )
            )
        if period.get("outside_units") is not None:
            rows.append(("Outside inverter", f"{period['outside_units']:g} units"))
    elif period:
        rows.append(("This period", f"{span}: not measurable yet"))
    previous = report.get("previous")
    if previous:
        rows.append(("Same days last month", f"{previous['consumed']:g} units used"))

    footer = "One unit is one kWh. Purchases are top-ups, not consumption; a period needs a reading at each end."
    notes = " ".join(report.get("notes") or [])
    if notes:
        footer = f"{footer} {notes}"
    return (
        '<article class="panel" id="utility-units">'
        f'<div class="eyebrow"><h2>Utility Units</h2>{_inline_badge(badge_text, badge_class)}</div>'
        f'<dl class="kv">{_kv_rows(rows)}</dl>'
        f'<p class="muted small">{esc(footer)}</p>'
        "</article>"
    )


def _load_profile_night_text(profile: dict[str, Any] | None) -> str:
    """One line comparing tonight's hourly-profile energy with the band planner."""
    if not profile:
        return "Not enough history yet."
    night = profile.get("night") or {}
    start = _clock_hhmm(night.get("start"))
    parts: list[str] = []
    hourly = night.get("hourly")
    if hourly:
        parts.append(
            f"hourly profile {hourly['median_wh'] / 1000:.1f} kWh "
            f"(likely {hourly['low_wh'] / 1000:.1f}–{hourly['high_wh'] / 1000:.1f})"
        )
    else:
        parts.append("hourly profile still learning")
    bands = night.get("bands")
    if bands:
        parts.append(f"band planner {bands['energy_wh'] / 1000:.1f} kWh")
    return f"Night from {start} for {night.get('hours', 12)}h: " + " · ".join(parts) + "."
