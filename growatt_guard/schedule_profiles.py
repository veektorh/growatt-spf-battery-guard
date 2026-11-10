from __future__ import annotations

import json
from typing import Any

from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.notifications import send_discord_message
from growatt_guard.schedule import (
    build_profiles_payload,
    format_outage_windows,
    load_schedule_document,
)
from growatt_guard.state import outage_mode, write_schedule_profile_state


def command_profiles(config: Any, json_output: bool = False) -> int:
    _ = config
    payload = build_profiles_payload()

    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if payload["active_profile"] is None:
        print("Schedule profiles: none (schedule.json uses a single job list).")
        return 0

    print(f"Active profile: {payload['label']} ({payload['active_profile']})")
    if payload["description"]:
        print(f"  {payload['description']}")
    print(f"  Outages: {payload['outages_text']}")
    print("Available profiles:")
    for profile in payload["profiles"]:
        marker = "*" if profile["active"] else " "
        print(
            f" {marker} {profile['name']:<16} {profile['label']} — "
            f"outages: {profile['outages_text']}"
        )
        if profile["description"]:
            print(f"   {' ' * 16} {profile['description']}")
    return 0


def _has_weekend_outage(outages: list[dict[str, str]]) -> bool:
    return any(window.get("days") in ("daily", "weekends") for window in outages)


def command_set_profile(config: Any, name: str, reason: str = "") -> int:
    document = load_schedule_document()
    profiles = document.get("profiles") or {}
    if not profiles:
        raise GrowattGuardError("schedule.json has no profiles; nothing to switch.")

    name = name.strip()
    if name not in profiles:
        available = ", ".join(profiles)
        raise GrowattGuardError(f"Unknown schedule profile: {name!r}. Available: {available}.")

    payload = build_profiles_payload(document)
    previous = payload["active_profile"]
    if name == previous:
        print(f"Schedule profile already {name}.")
        return 0

    write_schedule_profile_state(name, reason)
    body = profiles[name]
    message = (
        f"Growatt schedule profile set to {body['label']} (`{name}`, was `{previous}`).\n"
        f"{body['description']}\n"
        f"Outages: {format_outage_windows(body['outages'])}"
    )
    mode = outage_mode()
    if mode == "auto":
        message += "\nOutage-day gate: following the profile's outage windows."
    elif _has_weekend_outage(body["outages"]) and mode != "all":
        message += (
            f"\nWarning: outage-days mode is `{mode}`, so weekend preserve jobs will be skipped. "
            "Run `set-outage-days auto` to follow the profile, or `set-outage-days all` to run every day."
        )
    elif not _has_weekend_outage(body["outages"]) and mode == "all":
        message += (
            "\nNote: outage-days mode is `all`, so preserve jobs still run on weekends even though "
            "this profile declares no weekend cuts. Run `set-outage-days auto` to follow the profile."
        )
    if reason:
        message += f"\nReason: {reason}"
    send_discord_message(config, message)
    print(message)
    return 0
