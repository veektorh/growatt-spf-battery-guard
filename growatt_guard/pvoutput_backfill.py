from __future__ import annotations

import datetime as dt
import csv
import io
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests

from growatt_guard.exceptions import GrowattGuardError
from growatt_guard.paths import DATA_HOME
from growatt_guard.pvoutput import PVOUTPUT_GETOUTPUT_URL


PVOUTPUT_ADDOUTPUT_URL = "https://pvoutput.org/service/r2/addoutput.jsp"
PVOUTPUT_BACKFILL_REPORT_DIR = DATA_HOME / "reports"
_DATE_HEADERS = {"date", "day", "record date", "output date", "time"}
_GENERATION_HEADERS = {
    "generated",
    "generation",
    "generated energy",
    "generation energy",
    "energy",
    "energy generated",
    "pv generation",
    "etoday",
    "epvtoday",
}


@dataclass(frozen=True)
class BackfillRecord:
    date: str
    generated_wh: int


@dataclass(frozen=True)
class BackfillPlan:
    start_date: str
    through_date: str
    growatt_days: int
    growatt_generated_wh: int
    pvoutput_days: int
    missing_days: tuple[BackfillRecord, ...]
    matching_days: int
    conflicts: tuple[dict[str, Any], ...]
    completed_lifetime_wh: int | None
    reconciliation_delta_wh: int | None


def _error(message: str) -> GrowattGuardError:
    return GrowattGuardError(message)


def _energy_wh(value: Any, unit: str = "kwh") -> int | None:
    if isinstance(value, dict):
        for key in ("energy", "generated", "generation", "epv", "ppv", "value"):
            if key in value:
                return _energy_wh(value[key], unit=unit)
        return None
    if isinstance(value, bool) or value is None:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    try:
        amount = Decimal(match.group(0))
    except InvalidOperation:
        return None
    if amount < 0:
        return None
    multiplier = Decimal("1") if unit == "wh" else Decimal("1000")
    return int((amount * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_growatt_energy_history(payload: Any) -> dict[dt.date, int]:
    """Parse the official OpenAPI plant-energy response into daily Wh."""
    if not isinstance(payload, dict):
        raise _error("Growatt OpenAPI history returned a non-object response.")

    rows: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for nested in value.values():
                visit(nested)

    visit(payload)
    parsed: dict[dt.date, int] = {}
    for row in rows:
        label = next((row[key] for key in ("date", "day", "time") if key in row), None)
        energy_value = next(
            (row[key] for key in ("energy", "generated", "generation", "epvToday") if key in row),
            None,
        )
        if label is None or energy_value is None:
            continue
        try:
            day = dt.date.fromisoformat(str(label).strip()[:10].replace("/", "-"))
        except ValueError:
            continue
        energy = _energy_wh(energy_value)
        if energy is not None:
            parsed[day] = energy
    if parsed:
        return parsed
    if payload.get("count") in (0, "0"):
        return {}
    raise _error(
        "Growatt OpenAPI history did not contain recognized date/energy records. "
        f"Top-level keys: {', '.join(sorted(str(key) for key in payload)) or '(none)'}"
    )


def fetch_growatt_daily_generation(
    api: Any,
    plant_id: str,
    start_date: dt.date,
    end_date: dt.date,
) -> dict[dt.date, int]:
    result: dict[dt.date, int] = {}
    window_start = start_date
    while window_start <= end_date:
        window_end = min(end_date, window_start + dt.timedelta(days=6))
        payload = api.plant_energy_history(
            int(plant_id),
            start_date=window_start,
            end_date=window_end,
            time_unit="day",
            page=1,
            perpage=100,
        )
        for day, energy_wh in parse_growatt_energy_history(payload).items():
            if start_date <= day <= end_date:
                result[day] = energy_wh
        window_start = window_end + dt.timedelta(days=1)
    return result


def _normalized_header(value: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*", "", value.strip().lower()).replace("_", " ")


def read_growatt_csv(path: Path) -> dict[dt.date, int]:
    if path.suffix.lower() not in {".csv", ".txt"}:
        raise _error("Growatt history input must be CSV/text; export or save the ShineServer sheet as CSV first.")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise _error(f"Could not read Growatt history input: {exc}") from exc
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise _error("Growatt CSV has no header row.")
    headers = {_normalized_header(name): name for name in reader.fieldnames if name}
    date_header = next((headers[name] for name in _DATE_HEADERS if name in headers), None)
    energy_header = next((headers[name] for name in _GENERATION_HEADERS if name in headers), None)
    if not date_header or not energy_header:
        raise _error(
            "Growatt CSV needs a date column and a generated-energy column. "
            f"Found headers: {', '.join(reader.fieldnames)}"
        )
    energy_header_lower = energy_header.lower()
    energy_unit = "wh" if re.search(r"\(\s*wh\s*\)", energy_header_lower) else "kwh"
    result: dict[dt.date, int] = {}
    for line_number, row in enumerate(reader, start=2):
        raw_date = str(row.get(date_header, "")).strip()[:10]
        day = None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                day = dt.datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                continue
        energy = _energy_wh(row.get(energy_header), unit=energy_unit)
        if day is None or energy is None:
            raise _error(f"Invalid date or generated energy on Growatt CSV line {line_number}.")
        if day in result and result[day] != energy:
            raise _error(f"Conflicting Growatt generation values for {day.isoformat()} in CSV.")
        result[day] = energy
    if not result:
        raise _error("Growatt CSV contained no generation records.")
    return result


def _pvoutput_headers(config: Any) -> dict[str, str]:
    return {
        "X-Pvoutput-Apikey": config.pvoutput_api_key,
        "X-Pvoutput-SystemId": str(config.pvoutput_system_id),
        "X-Rate-Limit": "1",
    }


def fetch_existing_pvoutput_outputs(
    config: Any,
    start_date: dt.date,
    end_date: dt.date,
) -> dict[dt.date, int]:
    result: dict[dt.date, int] = {}
    window_start = start_date
    while window_start <= end_date:
        window_end = min(end_date, window_start + dt.timedelta(days=30))
        try:
            response = requests.get(
                PVOUTPUT_GETOUTPUT_URL,
                params={"df": window_start.strftime("%Y%m%d"), "dt": window_end.strftime("%Y%m%d")},
                headers=_pvoutput_headers(config),
                timeout=15,
            )
        except requests.RequestException as exc:
            raise _error(f"PVOutput history read failed: {exc}") from exc
        empty_response = response.status_code == 400 and any(
            message in response.text.lower()
            for message in ("no output", "no system or data found")
        )
        if empty_response:
            window_start = window_end + dt.timedelta(days=1)
            continue
        if response.status_code != 200:
            raise _error(f"PVOutput history read failed: HTTP {response.status_code} {response.text[:200]}")
        for record in response.text.replace(";", "\n").splitlines():
            parts = record.split(",")
            if len(parts) < 2:
                continue
            try:
                day = dt.datetime.strptime(parts[0].strip(), "%Y%m%d").date()
                result[day] = int(parts[1].strip())
            except (ValueError, IndexError):
                continue
        window_start = window_end + dt.timedelta(days=1)
    return result


def build_backfill_plan(
    growatt: dict[dt.date, int],
    pvoutput: dict[dt.date, int],
    start_date: dt.date,
    through_date: dt.date,
    completed_lifetime_wh: int | None,
) -> BackfillPlan:
    missing = tuple(
        BackfillRecord(day.isoformat(), energy)
        for day, energy in sorted(growatt.items())
        if day not in pvoutput
    )
    conflicts = tuple(
        {
            "date": day.isoformat(),
            "growatt_wh": energy,
            "pvoutput_wh": pvoutput[day],
            "delta_wh": energy - pvoutput[day],
        }
        for day, energy in sorted(growatt.items())
        if day in pvoutput and pvoutput[day] != energy
    )
    matching = sum(1 for day, energy in growatt.items() if pvoutput.get(day) == energy)
    growatt_total = sum(growatt.values())
    return BackfillPlan(
        start_date=start_date.isoformat(),
        through_date=through_date.isoformat(),
        growatt_days=len(growatt),
        growatt_generated_wh=growatt_total,
        pvoutput_days=len(pvoutput),
        missing_days=missing,
        matching_days=matching,
        conflicts=conflicts,
        completed_lifetime_wh=completed_lifetime_wh,
        reconciliation_delta_wh=(
            growatt_total - completed_lifetime_wh if completed_lifetime_wh is not None else None
        ),
    )


def _write_report(plan: BackfillPlan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(plan), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _report_matches(plan: BackfillPlan, path: Path) -> bool:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    expected = json.loads(json.dumps(asdict(plan)))
    return existing == expected


def _format_kwh(wh: int | None) -> str:
    return "unavailable" if wh is None else f"{wh / 1000:,.1f} kWh"


def _upload_records(config: Any, records: tuple[BackfillRecord, ...]) -> int:
    def post(data: dict[str, str]) -> requests.Response:
        try:
            return requests.post(
                PVOUTPUT_ADDOUTPUT_URL,
                data=data,
                headers=_pvoutput_headers(config),
                timeout=15,
            )
        except requests.RequestException as exc:
            raise _error(f"PVOutput backfill request failed: {exc}") from exc

    uploaded = 0
    batches = tuple(records[index:index + 100] for index in range(0, len(records), 100))
    for batch in batches:
        csv_data = ";".join(
            f"{record.date.replace('-', '')},{record.generated_wh}" for record in batch
        )
        response = post({"data": csv_data})
        if response.status_code == 200:
            uploaded += len(batch)
            continue

        batch_unavailable = len(batch) > 1 and response.status_code in {400, 403} and any(
            marker in response.text.lower() for marker in ("batch", "donat", "multiple")
        )
        if batch_unavailable:
            for record in batch:
                response = post({"d": record.date.replace("-", ""), "g": str(record.generated_wh)})
                if response.status_code != 200:
                    break
                uploaded += 1
            else:
                continue

        remaining = response.headers.get("X-Rate-Limit-Remaining", "unknown")
        reset = response.headers.get("X-Rate-Limit-Reset", "unknown")
        raise _error(
            f"PVOutput backfill stopped after {uploaded} uploads: HTTP {response.status_code} "
            f"{response.text[:200]} (rate remaining={remaining}, reset={reset}). "
            "Re-run the same command later; existing dates will be skipped."
        )
    return uploaded


def command_pvoutput_backfill(
    config: Any,
    from_date: str,
    through_date: str,
    apply: bool,
    overwrite_conflicts: bool,
    output: str,
    input_path: str,
) -> int:
    if not config.pvoutput_enabled or not config.pvoutput_api_key or not config.pvoutput_system_id:
        raise _error("PVOUTPUT_ENABLED=true, PVOUTPUT_API_KEY, and PVOUTPUT_SYSTEM_ID are required.")
    if overwrite_conflicts and not apply:
        raise _error("--overwrite-conflicts requires --apply.")

    source_records: dict[dt.date, int] | None = None
    if input_path:
        source_records = read_growatt_csv(Path(input_path))

    if from_date:
        try:
            start = dt.date.fromisoformat(from_date)
        except ValueError as exc:
            raise _error("--from must use YYYY-MM-DD.") from exc
    elif source_records:
        start = min(source_records)
    else:
        raise _error("--from YYYY-MM-DD is required when reading Growatt OpenAPI history.")

    if through_date:
        try:
            through = dt.date.fromisoformat(through_date)
        except ValueError as exc:
            raise _error("--through must use YYYY-MM-DD.") from exc
    else:
        through = dt.date.today() - dt.timedelta(days=1)
    if start > through:
        raise _error("Backfill start date must not be after the through date.")

    if source_records is not None:
        growatt = {day: energy for day, energy in source_records.items() if start <= day <= through}
    else:
        if not config.growatt_api_token:
            raise _error(
                "This SPF plant's legacy history API is not authoritative. Set GROWATT_API_TOKEN for the "
                "official API, or pass a ShineServer CSV export with --input."
            )
        if not config.plant_id:
            raise _error("GROWATT_PLANT_ID is required for official OpenAPI history.")
        try:
            from growattServer import OpenApiV1
        except ImportError as exc:  # pragma: no cover - dependency check happens earlier
            raise _error("growattServer is required for PVOutput backfill.") from exc
        api = OpenApiV1(token=config.growatt_api_token)
        api.api_url = config.server_url.rstrip("/") + "/v1/"
        growatt = fetch_growatt_daily_generation(api, config.plant_id, start, through)
    if not growatt:
        raise _error("Growatt returned no daily generation records for the requested range.")
    pvoutput = fetch_existing_pvoutput_outputs(config, start, through)
    plan = build_backfill_plan(growatt, pvoutput, start, through, None)

    report_path = Path(output) if output else PVOUTPUT_BACKFILL_REPORT_DIR / "pvoutput-backfill-preview.json"
    preview_matches = _report_matches(plan, report_path)
    _write_report(plan, report_path)
    print(f"Growatt: {plan.growatt_days} days, {_format_kwh(plan.growatt_generated_wh)}")
    print(f"PVOutput: {plan.matching_days} matching, {len(plan.missing_days)} missing, {len(plan.conflicts)} conflicts")
    if plan.completed_lifetime_wh is not None:
        print(
            "Lifetime reconciliation: "
            f"counter={_format_kwh(plan.completed_lifetime_wh)}, "
            f"daily-sum delta={_format_kwh(plan.reconciliation_delta_wh)}"
        )
    print(f"Preview report: {report_path}")

    records = plan.missing_days
    if overwrite_conflicts:
        records += tuple(
            BackfillRecord(item["date"], item["growatt_wh"])
            for item in plan.conflicts
        )
    if not apply:
        print("Preview only; re-run with --apply to upload missing days.")
        return 0
    if not preview_matches:
        raise _error(
            "No matching previously reviewed preview was found. Review the report, then re-run the same --apply command."
        )
    if config.dry_run:
        raise _error("--apply is blocked while DRY_RUN=true. Set DRY_RUN=false only after reviewing the preview.")
    if not records:
        print("PVOutput is already complete for the requested range; nothing uploaded.")
        return 0

    uploaded = _upload_records(config, records)
    print(f"PVOutput backfill complete: uploaded {uploaded} daily outputs.")
    return 0
