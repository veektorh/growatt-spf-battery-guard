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
