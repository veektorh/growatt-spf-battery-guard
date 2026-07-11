from __future__ import annotations

from importlib.resources import files


def _asset_text(name: str) -> str:
    return files("growatt_guard").joinpath("assets", name).read_text(encoding="utf-8")


DASHBOARD_CSS = _asset_text("dashboard.css")
DASHBOARD_JS = _asset_text("dashboard.js")
