#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


SENSITIVE_ENV_KEYS = {
    "GROWATT_USERNAME",
    "GROWATT_PASSWORD",
    "GROWATT_PLANT_ID",
    "GROWATT_DEVICE_SN",
    "WEATHER_LAT",
    "WEATHER_LON",
    "DISCORD_WEBHOOK_URL",
    "DISCORD_BOT_TOKEN",
    "DISCORD_CONTROL_CHANNEL_ID",
    "DISCORD_CONTROL_ALLOWED_USER_IDS",
    "DISCORD_CONTROL_GUILD_ID",
    "PVOUTPUT_API_KEY",
    "PVOUTPUT_SYSTEM_ID",
    "BETTERSTACK_HEARTBEAT_URL",
}

ENV_ASSIGNMENT_RE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*([^#\s]*)")
REAL_DISCORD_WEBHOOK_RE = re.compile(r"discord\.com/api/webhooks/[0-9]")


@dataclass(frozen=True)
class HygieneViolation:
    path: str
    line_number: int
    message: str

    def format(self) -> str:
        return f"{self.path}:{self.line_number}: {self.message}"


def _placeholder_value(value: str) -> bool:
    cleaned = value.strip().strip('"\'')
    if cleaned == "":
        return True
    lowered = cleaned.lower()
    if "..." in cleaned or "[redacted]" in lowered:
        return True
    if lowered.startswith("your_") or lowered in {"example", "example.invalid", "placeholder"}:
        return True
    return False


def find_violations_in_text(path: str, text: str) -> list[HygieneViolation]:
    violations: list[HygieneViolation] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if REAL_DISCORD_WEBHOOK_RE.search(line):
            violations.append(HygieneViolation(path, index, "possible real Discord webhook URL"))

        match = ENV_ASSIGNMENT_RE.match(line)
        if not match:
            continue
        key, value = match.groups()
        if key not in SENSITIVE_ENV_KEYS:
            continue
        if not _placeholder_value(value):
            violations.append(HygieneViolation(path, index, f"possible real value for {key}"))
    return violations


def tracked_files() -> list[Path]:
    result = subprocess.run(["git", "ls-files"], check=True, text=True, stdout=subprocess.PIPE)
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def find_violations(paths: list[Path]) -> list[HygieneViolation]:
    violations: list[HygieneViolation] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        violations.extend(find_violations_in_text(str(path), text))
    return violations


def main() -> int:
    violations = find_violations(tracked_files())
    if not violations:
        print("Public hygiene OK: no likely secrets or private identifiers in tracked files.")
        return 0

    print("Public hygiene check failed:", file=sys.stderr)
    for violation in violations:
        print(violation.format(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
