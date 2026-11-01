"""Small Garmin daily summaries, kept separate from Hevy and user reports."""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Annotated, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator


# Flat, whitelisted summaries only; shared by storage visibility and retrieval.
BASE_FIELDS = ("steps", "sleep_seconds", "resting_hr_bpm")
RECOVERY_FIELDS = ("average_stress_level", "body_battery_highest", "body_battery_lowest",
    "body_battery_at_wake", "body_battery_most_recent", "hrv_last_night_ms", "hrv_weekly_avg_ms",
    "hrv_last_night_5min_high_ms", "hrv_status")
SLEEP_FIELDS = ("deep_sleep_seconds", "light_sleep_seconds", "rem_sleep_seconds", "awake_sleep_seconds", "nap_sleep_seconds")
BREATHING_FIELDS = ("waking_respiration_brpm", "sleeping_respiration_brpm", "spo2_average_pct",
    "spo2_lowest_pct", "spo2_latest_pct", "spo2_sleep_average_pct")
SCALAR_FIELDS = BASE_FIELDS + RECOVERY_FIELDS + SLEEP_FIELDS + BREATHING_FIELDS
METRIC_GROUP_FIELDS = {
    "recovery": RECOVERY_FIELDS,
    "sleep": SLEEP_FIELDS,
    "breathing": BREATHING_FIELDS,
    "all": RECOVERY_FIELDS + SLEEP_FIELDS + BREATHING_FIELDS,
}
SLEEP_SCORE_LABELS = {
    "overall": ("Overall sleep score", " / 100"),
    "remPercentage": ("REM share of sleep", "%"),
    "lightPercentage": ("Light sleep share", "%"),
    "deepPercentage": ("Deep sleep share", "%"),
}
Score = Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)]
ScoreName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")]


def has_metrics(row):
    return any(row.get(k) is not None for k in SCALAR_FIELDS) or bool(row.get("sleep_scores"))


class GarminDay(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: date
    provider: str = "garmin"
    steps: int | None = Field(default=None, ge=0, le=1000000, strict=True)
    sleep_seconds: int | None = Field(default=None, ge=0, le=86400, strict=True)
    resting_hr_bpm: float | None = Field(default=None, gt=0, le=300, allow_inf_nan=False, strict=True)
    average_stress_level: int | None = Field(default=None, ge=0, le=100, strict=True)
    body_battery_highest: int | None = Field(default=None, ge=0, le=100, strict=True)
    body_battery_lowest: int | None = Field(default=None, ge=0, le=100, strict=True)
    body_battery_at_wake: int | None = Field(default=None, ge=0, le=100, strict=True)
    body_battery_most_recent: int | None = Field(default=None, ge=0, le=100, strict=True)
    hrv_last_night_ms: float | None = Field(default=None, gt=0, le=10000, allow_inf_nan=False, strict=True)
    hrv_weekly_avg_ms: float | None = Field(default=None, gt=0, le=10000, allow_inf_nan=False, strict=True)
    hrv_last_night_5min_high_ms: float | None = Field(default=None, gt=0, le=10000, allow_inf_nan=False, strict=True)
    hrv_status: Literal["POOR", "LOW", "UNBALANCED", "BALANCED"] | None = None
    waking_respiration_brpm: float | None = Field(default=None, gt=0, le=200, allow_inf_nan=False, strict=True)
    sleeping_respiration_brpm: float | None = Field(default=None, gt=0, le=200, allow_inf_nan=False, strict=True)
    spo2_average_pct: float | None = Field(default=None, gt=0, le=100, allow_inf_nan=False, strict=True)
    spo2_lowest_pct: float | None = Field(default=None, gt=0, le=100, allow_inf_nan=False, strict=True)
    spo2_latest_pct: float | None = Field(default=None, gt=0, le=100, allow_inf_nan=False, strict=True)
    spo2_sleep_average_pct: float | None = Field(default=None, gt=0, le=100, allow_inf_nan=False, strict=True)
    sleep_scores: dict[ScoreName, Score] = Field(default_factory=dict, max_length=8)
    deep_sleep_seconds: int | None = Field(default=None, ge=0, le=86400, strict=True)
    light_sleep_seconds: int | None = Field(default=None, ge=0, le=86400, strict=True)
    rem_sleep_seconds: int | None = Field(default=None, ge=0, le=86400, strict=True)
    awake_sleep_seconds: int | None = Field(default=None, ge=0, le=86400, strict=True)
    nap_sleep_seconds: int | None = Field(default=None, ge=0, le=86400, strict=True)

    @field_validator("sleep_scores", mode="before")
    @classmethod
    def missing_sleep_scores(cls, value):
        return {} if value is None else value


def normalize_days(rows, start: date, end: date):
    if not isinstance(rows, list) or len(rows) > 31:
        raise ValueError("Garmin daily response exceeded its bounded range")
    result, seen = [], set()
    for row in rows:
        item = GarminDay.model_validate(row)
        if item.provider != "garmin" or not start <= item.date <= end or item.date in seen:
            raise ValueError("Invalid Garmin daily date/source")
        seen.add(item.date)
        result.append(item.model_dump(mode="json"))
    return result


def sync_garmin(database, client, *, now=None, timezone_name="America/Toronto"):
    """Fetch/validate everything before writes; a failure never claims a fresh sync."""
    now = now or datetime.now(timezone.utc)
    at = now.isoformat()
    end = now.astimezone(ZoneInfo(timezone_name)).date()
    start = end - timedelta(days=6)
    state = database.get_garmin_state()
    database.save_garmin_state({**state, "last_attempt_at": at, "last_error": None})
    try:
        rows = normalize_days(client.fetch_range(start, end), start, end)
        database.upsert_garmin_days(rows, at)
    except Exception:
        # API and DB exceptions may contain sensitive details. Store only fixed copy.
        database.save_garmin_state({
            **state,
            "last_attempt_at": at,
            "last_error": "Garmin sync did not finish. Previous saved data is still available; check the connection and try again.",
        })
        return {"status": "failed", "days": 0}
    finished = datetime.now(timezone.utc).isoformat()
    database.save_garmin_state({
        "last_attempt_at": at,
        "last_success_at": finished,
        "last_error": None,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "days_received": sum(has_metrics(row) for row in rows),
    })
    return {"status": "success", "days": len(rows)}


def garmin_context(database, *, days=3, metrics="recovery"):
    """One bounded lookup; send only the metric group relevant to the question."""
    if metrics not in METRIC_GROUP_FIELDS:
        metrics = "recovery"
    extra_fields = METRIC_GROUP_FIELDS[metrics]
    rows = database.list_garmin_days(min(7, max(1, days)))
    state = database.get_garmin_state()
    summaries = []
    for row in rows:
        item = {key: row.get(key) for key in ("date", *BASE_FIELDS, "synced_at")}
        item.update({key: row[key] for key in extra_fields if row.get(key) is not None})
        sleep_seconds = row.get("sleep_seconds")
        item["sleep_hours"] = round(sleep_seconds / 3600, 1) if sleep_seconds is not None else None
        if metrics in {"sleep", "all"}:
            item["sleep_scores"] = row.get("sleep_scores") or {}
            for key in SLEEP_FIELDS:
                if row.get(key) is not None:
                    item[key.replace("_seconds", "_minutes")] = round(row[key] / 60, 1)
        summaries.append(item)
    return {"source": "garmin_connect", "record_type": "wearable_estimates_not_hevy_or_user_report",
            "metric_group": metrics, "last_success_at": state.get("last_success_at"),
            "sync_error": bool(state.get("last_error")), "days": summaries,
            "limits": "Saved daily estimates, not live monitoring or Garmin Training Readiness. Missing/omitted metrics are unknown. HRV in ms, respiration in breaths/min, SpO2 in percent, stress/Body Battery on Garmin scales. sleep_scores may include stage percentages alongside an overall score: percentages are not quality scores. Sleep stages exclude naps; dates are Garmin calendar dates. Do not diagnose, infer readiness scores, or treat estimates as confirmed lifts."}


def _duration(seconds):
    if seconds is None:
        return "—"
    hours, minutes = divmod(int(seconds) // 60, 60)
    return f"{hours} h {minutes:02d} min"


def _reading(row, key, suffix=""):
    value = row.get(key)
    return "—" if value is None else f"{value:g}{suffix}"


def _sleep_score_readings(scores):
    # The official map has no guaranteed keys. Label known returned keys;
    # unfamiliar components retain their name/value without guessing a unit.
    readings = []
    for key, value in (scores or {}).items():
        label, suffix = SLEEP_SCORE_LABELS.get(key, (f"Garmin sleep metric · {key}", ""))
        readings.append((label, f"{value:g}{suffix}"))
    return readings


def garmin_page(database):
    rows = database.list_garmin_days(7)
    for row in rows:
        seconds = row.get("sleep_seconds")
        row["sleep_display"] = f"{seconds / 3600:.1f} h" if seconds is not None else "—"
        row["sleep_detail"] = _duration(seconds)
        row["recovery_readings"] = [(label, _reading(row, key, suffix)) for label, key, suffix in (
            ("Overnight HRV", "hrv_last_night_ms", " ms"), ("Weekly HRV", "hrv_weekly_avg_ms", " ms"),
            ("Highest 5-min HRV", "hrv_last_night_5min_high_ms", " ms"),
            ("Average stress", "average_stress_level", " / 100"),
            ("Body Battery at wake", "body_battery_at_wake", " / 100"),
            ("Latest Body Battery", "body_battery_most_recent", " / 100"),
            ("Highest Body Battery", "body_battery_highest", " / 100"),
            ("Lowest Body Battery", "body_battery_lowest", " / 100"))]
        hrv_status = row.get("hrv_status")
        row["hrv_status_display"] = hrv_status.replace("_", " ").title() if hrv_status else "—"
        row["sleep_readings"] = [(label, _duration(row.get(key))) for label, key in (
            ("Deep sleep", "deep_sleep_seconds"), ("Light sleep", "light_sleep_seconds"),
            ("REM sleep", "rem_sleep_seconds"), ("Awake time", "awake_sleep_seconds"),
            ("Naps", "nap_sleep_seconds"))]
        row["sleep_score_readings"] = _sleep_score_readings(row.get("sleep_scores"))
        row["breathing_readings"] = [(label, _reading(row, key, suffix)) for label, key, suffix in (
            ("Waking respiration", "waking_respiration_brpm", " breaths/min"),
            ("Sleep respiration", "sleeping_respiration_brpm", " breaths/min"),
            ("Average blood oxygen", "spo2_average_pct", "%"),
            ("Sleep blood oxygen", "spo2_sleep_average_pct", "%"),
            ("Lowest blood oxygen", "spo2_lowest_pct", "%"),
            ("Latest blood oxygen", "spo2_latest_pct", "%"))]
        row["has_blood_oxygen"] = any(row.get(key) is not None for key in BREATHING_FIELDS if key.startswith("spo2_"))
    return {"state": database.get_garmin_state(), "days": rows, "latest": rows[0] if rows else None}
