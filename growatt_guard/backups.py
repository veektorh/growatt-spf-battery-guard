from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import growatt_guard.audit as audit_module
import growatt_guard.dashboard_metrics as metrics_module
import growatt_guard.schedule_overrides as overrides_module
import growatt_guard.state as state_module
from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.growatt_api import extract_spf_output_source, load_context
from growatt_guard.schedule import validate_schedule, validate_schedule_overrides

BASE_DIR = Path(__file__).resolve().parents[1]
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_SCHEMA_VERSION = 1
MAX_RESTORED_HOLD_HOURS = 24


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GrowattGuardError(f"Could not read {label}: {exc}") from exc


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except OSError as exc:
        raise GrowattGuardError(f"Could not read {label}: {exc}") from exc
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GrowattGuardError(f"Invalid {label} row {index}: {exc}") from exc
        if not isinstance(row, dict):
            raise GrowattGuardError(f"Invalid {label} row {index}: expected an object.")
        rows.append(row)
    return rows


def _atomic_write_text(path: Path, content: str, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        if private:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_backup_payload(*, include_active_hold: bool = False) -> dict[str, Any]:
    sections: dict[str, Any] = {}
    if overrides_module.SCHEDULE_OVERRIDES_FILE.exists():
        sections["schedule_overrides"] = _read_json(
            overrides_module.SCHEDULE_OVERRIDES_FILE, "schedule overrides"
        )
    if audit_module.MODE_AUDIT_FILE.exists():
        sections["mode_audit"] = audit_module.read_mode_audit_rows()
    if metrics_module.DASHBOARD_METRICS_FILE.exists():
        sections["dashboard_metrics"] = _read_jsonl(
            metrics_module.DASHBOARD_METRICS_FILE, "dashboard metrics"
        )
    forecasts = state_module.read_forecast_calibration_history()
    if forecasts:
        sections["forecast_calibration"] = forecasts
    hold = state_module.read_utility_hold_state()
    if include_active_hold and hold is not None:
        sections["utility_hold"] = hold

    return {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "includes_active_hold": "utility_hold" in sections,
        "sections": sections,
    }


def command_backup_state(output: str = "", include_active_hold: bool = False) -> int:
    payload = build_backup_payload(include_active_hold=include_active_hold)
    if output:
        output_path = Path(output).expanduser()
        if not output_path.is_absolute():
            output_path = BASE_DIR / output_path
    else:
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        output_path = BACKUP_DIR / f"growatt-guard-{stamp}.backup.json"
    _atomic_write_text(output_path, json.dumps(payload, indent=2, sort_keys=True) + "\n", private=True)
    section_names = ", ".join(sorted(payload["sections"])) or "none"
    print(f"Backup written to {output_path} (sections: {section_names}).")
    if not include_active_hold:
        print("Active Utility hold excluded; use --include-active-hold only for deliberate recovery snapshots.")
    return 0


def _validated_hold(hold: Any, now: dt.datetime) -> dict[str, Any]:
    if not isinstance(hold, dict):
        raise GrowattGuardError("Backup utility_hold must be an object.")
    required = {"ownership", "completion_policy", "started_at", "max_expiry"}
    missing = sorted(required - set(hold))
    if missing:
        raise GrowattGuardError(f"Backup Utility hold is missing: {', '.join(missing)}")
    try:
        normalized = state_module.UtilityHold.from_state(hold).to_state()
        expiry = state_module.parse_utc_datetime(str(normalized["max_expiry"]))
        started = state_module.parse_utc_datetime(str(normalized["started_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise GrowattGuardError(f"Backup Utility hold is invalid: {exc}") from exc
    if normalized["ownership"] not in {"owned", "adopted"}:
        raise GrowattGuardError("Backup Utility hold ownership must be owned or adopted.")
    if normalized["completion_policy"] not in {"soc", "time"}:
        raise GrowattGuardError("Backup Utility hold completion policy is unsupported.")
    if expiry <= now:
        raise GrowattGuardError("Backup Utility hold has expired and cannot be restored.")
    if expiry > now + dt.timedelta(hours=MAX_RESTORED_HOLD_HOURS):
        raise GrowattGuardError(
            f"Backup Utility hold expires more than {MAX_RESTORED_HOLD_HOURS} hours ahead."
        )
    if started > now + dt.timedelta(minutes=5) or started > expiry:
        raise GrowattGuardError("Backup Utility hold timestamps are inconsistent.")
    target = normalized.get("target_soc")
    if target is not None and not 0 <= float(target) <= 100:
        raise GrowattGuardError("Backup Utility hold target_soc must be between 0 and 100.")
    if normalized["completion_policy"] == "soc" and target is None:
        raise GrowattGuardError("SOC-based backup Utility hold requires target_soc.")
    minutes = normalized.get("minutes")
    if normalized["completion_policy"] == "time" and (
        minutes is None or not 0 < int(minutes) <= MAX_RESTORED_HOLD_HOURS * 60
    ):
        raise GrowattGuardError("Time-based backup Utility hold requires bounded positive minutes.")
    return normalized


def _validate_restore_sections(sections: Any) -> dict[str, Any]:
    if not isinstance(sections, dict):
        raise GrowattGuardError("Backup sections must be an object.")
    allowed = {
        "schedule_overrides", "mode_audit", "dashboard_metrics",
        "forecast_calibration", "utility_hold",
    }
    unknown = sorted(set(sections) - allowed)
    if unknown:
        raise GrowattGuardError(f"Backup contains unsupported section(s): {', '.join(unknown)}")
    for name in ("mode_audit", "dashboard_metrics", "forecast_calibration"):
        value = sections.get(name)
        if value is not None and (not isinstance(value, list) or not all(isinstance(row, dict) for row in value)):
            raise GrowattGuardError(f"Backup {name} must be a list of objects.")
    return sections


def _validate_overrides(overrides: Any) -> None:
    if not isinstance(overrides, dict):
        raise GrowattGuardError("Backup schedule_overrides must be an object.")
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "schedule_overrides.json"
        path.write_text(json.dumps(overrides), encoding="utf-8")
        validate_schedule_overrides(validate_schedule(), path=path)


def _render_audit_csv(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=audit_module.MODE_AUDIT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def command_restore_state(config: Any, input_path: str, allow_active_hold: bool = False) -> int:
    path = Path(input_path).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    payload = _read_json(path, "backup")
    if not isinstance(payload, dict) or payload.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise GrowattGuardError(f"Backup schema_version must be {BACKUP_SCHEMA_VERSION}.")
    sections = _validate_restore_sections(payload.get("sections"))

    overrides = sections.get("schedule_overrides")
    if overrides is not None:
        _validate_overrides(overrides)
    metrics = sections.get("dashboard_metrics")
    if metrics is not None and not all(row.get("timestamp") for row in metrics):
        raise GrowattGuardError("Every dashboard metric row must include timestamp.")

    hold = sections.get("utility_hold")
    normalized_hold = None
    if hold is not None:
        if not allow_active_hold:
            raise GrowattGuardError("Backup contains an active Utility hold; pass --allow-active-hold to validate and restore it.")
        if state_module.read_utility_hold_state() is not None or state_module.read_topup_state() is not None:
            raise GrowattGuardError("Existing Utility hold/topup state must be resolved before restoring another hold.")
        normalized_hold = _validated_hold(hold, state_module.utc_now())
        _, _, status = load_context(config)
        output_source = extract_spf_output_source(status)
        if output_source is None or output_source[0] != "2":
            raise GrowattGuardError("Active Utility hold restore requires a live read confirming Utility first [2].")

    if overrides is not None:
        _atomic_write_text(
            overrides_module.SCHEDULE_OVERRIDES_FILE,
            json.dumps(overrides, indent=2, sort_keys=True) + "\n",
        )
    audit_rows = sections.get("mode_audit")
    if audit_rows is not None:
        _atomic_write_text(audit_module.MODE_AUDIT_FILE, _render_audit_csv(audit_rows))
    if metrics is not None:
        content = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in metrics)
        _atomic_write_text(metrics_module.DASHBOARD_METRICS_FILE, content)
    forecasts = sections.get("forecast_calibration")
    if forecasts is not None:
        state_module.write_forecast_calibration_history(forecasts)
    if normalized_hold is not None:
        state_module.write_json_state(state_module.UTILITY_HOLD_FILE, normalized_hold)

    restored = ", ".join(sorted(sections)) or "none"
    print(f"Restored backup sections: {restored}.")
    return 0
