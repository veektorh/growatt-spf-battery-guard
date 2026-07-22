from __future__ import annotations

from importlib.resources import files


def _asset_text(name: str) -> str:
    return files("growatt_guard").joinpath("assets", name).read_text(encoding="utf-8")


def _asset_bytes(name: str) -> bytes:
    return files("growatt_guard").joinpath("assets", name).read_bytes()


DASHBOARD_CSS = _asset_text("dashboard.css")
DASHBOARD_JS = _asset_text("dashboard.js")
DASHBOARD_MANIFEST = _asset_bytes("manifest.webmanifest")
DASHBOARD_ICON_SVG = _asset_bytes("dashboard-icon.svg")
DASHBOARD_ICON_180 = _asset_bytes("dashboard-icon-180.png")
DASHBOARD_ICON_192 = _asset_bytes("dashboard-icon-192.png")
DASHBOARD_ICON_512 = _asset_bytes("dashboard-icon-512.png")
DASHBOARD_ICON_MASKABLE_512 = _asset_bytes("dashboard-icon-maskable-512.png")
