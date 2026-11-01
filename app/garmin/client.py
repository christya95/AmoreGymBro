"""Small read-only adapter for the official Garmin Connect Personal API.

Public sources checked 2026-11-01 (OpenAPI 3.0.1, API version 1.0.0):
https://developer.garmin.com/gc-developer-program/personal/swagger-docs/
https://developer.garmin.com/component---src-pages-gc-developer-program-personal-swagger-docs-js-715115a6210d45afc282.js
https://developer.garmin.com/gc-developer-program/personal/personal-faq/
https://github.com/garmin/connect-personal-api-python/blob/main/src/garmin_personal_api/auth.py

The Gatsby component embeds the actual OpenAPI document as JSON.parse(...).
GET /usersummary-service/usersummary/daily?calendarDate=YYYY-MM-DD returns
UserDailySummaryDto: totalSteps, restingHeartRate, averageStressLevel,
bodyBatteryHighestValue and bodyBatteryLowestValue. Unlike V2's restingValue,
restingHeartRate is not documented to fall back to minimum heart rate.
The current Swagger page still links the exact component URL above; its escaped
JavaScript string literal must be decoded before parsing the embedded JSON.
GET /sleep-service/sleep/dailySleepSummariesByDate?startDate=...&endDate=...
returns DailySleepDTO[] for at most 31 days: calendarDate, sleepTimeSeconds.
GET /sleep-service/sleep/dailySleep?date=... supplies the detailed DailySleepDTO:
deepSleepSeconds, lightSleepSeconds, remSleepSeconds, awakeSleepSeconds,
napTimeSeconds, averageRespirationValue and sleepScores.*.value. Score names are
not enumerated by the schema; no particular component (including overall) is
guaranteed. Total sleep and stages refer to the main sleep window, excluding
naps. napTimeSeconds is the separate calendar-date sum. Preserve the sleep's
associated calendarDate; do not infer a date from timestamps or shift UTC bins.
UserDailySummaryDto also supplies bodyBatteryAtWakeTime,
bodyBatteryMostRecentValue and avgWakingRespirationValue.
GET /wellness-service/wellness/daily/spo2acclimation/{calendarDate} returns
DailySpO2AndAcclimationDTO: averageSpO2, lowestSpO2, latestSpO2, avgSleepSpO2.
These retain that DTO's calendarDate; avgTomorrowSleepSpO2 is deliberately
excluded, and no equivalence to DailySleepDTO.averageSpO2HRSleep is assumed.
GET /hrv-service/hrv/daily/{fromDate}/{toDate} returns HrvDailySummaryDTO:
hrvSummaries[].{calendarDate,lastNightAvg,weeklyAvg,lastNight5MinHigh,status}.
The official Garmin Python client's range wrapper confirms inclusive bounds.
Only this HRV range endpoint documents HTTP 204 as no data. No HRV chart/sample
endpoint is requested; incidental SpO2 sample arrays are discarded unread.
Reference wrappers: https://github.com/garmin/connect-personal-api-python/blob/main/src/garmin_personal_api/client.py

Only authentication uses POST, at the FAQ's personal-api-key/token endpoint.
The official reference client reads a plain-text JWT with an exp claim. That
claim is decoded only to shorten caching, never to authorize data. Tokens live
only in memory, for at most 24 hours minus a safety margin; no refresh token.

fetch_range returns ascending ISO date strings, inclusive, including empty days.
Metrics are schema-typed integers/floats or None, with provider='garmin'; only
allowlisted HRV statuses and up to eight identifier-keyed numeric sleep scores
are returned. Unknown fields, identifiers, notes, samples and credentials are
discarded. Activities and TrainingReadiness are outside this adapter.
Every source must succeed and validate before any result is returned. An empty
sleep list or absent/null scalar means missing data; HTTP errors (including 404
and undocumented 204 responses) never silently become empty successful reads.
Ranges are capped locally at 366 days; sleep summaries and HRV are split into
31-day chunks (a local HRV limit, not a claimed provider requirement). A range
uses 3*N + 2*ceil(N/31) GETs before bounded retries/renewal. Sleep summaries
remain the duration source; detailed sleep supplements the other sleep fields.
Use a separate client per worker; instances are synchronous, not shared.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
SUMMARY_PATH = "/usersummary-service/usersummary/daily"
SLEEP_PATH = "/sleep-service/sleep/dailySleepSummariesByDate"
SLEEP_DETAIL_PATH = "/sleep-service/sleep/dailySleep"
SPO2_PATH = "/wellness-service/wellness/daily/spo2acclimation/{calendarDate}"
HRV_PATH = "/hrv-service/hrv/daily/{fromDate}/{toDate}"
MAX_RANGE_DAYS = 366
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER = 30.0
TOKEN_MARGIN = 60.0
_KINDS = frozenset({"auth", "rate_limit", "unavailable", "http", "invalid_response", "invalid_range", "closed"})
_SOURCES = frozenset({"auth", "daily_summary", "sleep", "spo2", "hrv"})
_STATUSES = frozenset({400, 401, 403, 404, 408, 413, 422, 429, 500, 501, 502, 503, 504})
_SUMMARY_FIELDS = {
    "steps": ("totalSteps", 0, 2**31 - 1),
    "resting_hr_bpm": ("restingHeartRate", 1, 2**31 - 1),
    "average_stress_level": ("averageStressLevel", 0, 100),
    "body_battery_highest": ("bodyBatteryHighestValue", 0, 100),
    "body_battery_lowest": ("bodyBatteryLowestValue", 0, 100),
    "body_battery_at_wake": ("bodyBatteryAtWakeTime", 0, 100),
    "body_battery_most_recent": ("bodyBatteryMostRecentValue", 0, 100),
}
_SLEEP_FIELDS = {
    "deep_sleep_seconds": "deepSleepSeconds",
    "light_sleep_seconds": "lightSleepSeconds",
    "rem_sleep_seconds": "remSleepSeconds",
    "awake_sleep_seconds": "awakeSleepSeconds",
    "nap_sleep_seconds": "napTimeSeconds",
}
_HRV_FIELDS = {
    "hrv_last_night_ms": "lastNightAvg",
    "hrv_weekly_avg_ms": "weeklyAvg",
    "hrv_last_night_5min_high_ms": "lastNight5MinHigh",
}
_HRV_STATUSES = frozenset({"NONE", "POOR", "LOW", "UNBALANCED", "BALANCED", "INVALID"})
_NO_HRV_DATA = object()
_DATED_GET_PATH = re.compile(
    r"(?:/wellness-service/wellness/daily/spo2acclimation/[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|/hrv-service/hrv/daily/[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]{4}-[0-9]{2}-[0-9]{2})"
)


class GarminError(Exception):
    """Safe category only: no response body, request, URL, or chained exception.

    kind, status_code and source are allowlisted. A failed fetch returns no days;
    callers must not mark the requested interval as successfully synchronized.
    """

    def __init__(self, kind: str, *, status_code: int | None = None, source: str | None = None):
        self.kind = kind if kind in _KINDS else "unavailable"
        self.status_code = status_code if type(status_code) is int and status_code in _STATUSES else None
        self.source = source if source in _SOURCES else None
        super().__init__(f"Garmin {self.kind}.")


def _metric(item: dict, field: str, minimum: int, maximum: int, source: str) -> int | None:
    value = item.get(field)
    if value is None:
        return None
    if type(value) is not int:
        raise GarminError("invalid_response", source=source)
    # Negative provider sentinels and physiologically absent zero HR remain
    # missing, never fabricated zero-valued measurements.
    if value < minimum:
        return None
    if value > maximum:
        raise GarminError("invalid_response", source=source)
    return value


def _number_metric(item: dict, field: str, maximum: float, source: str) -> float | None:
    """OpenAPI number fields retain decimals; zero/negative readings are absent."""
    value = item.get(field)
    if value is None:
        return None
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise GarminError("invalid_response", source=source)
    if value <= 0:
        return None
    # Bound before conversion so huge integers cannot cause OverflowError.
    if value > maximum:
        raise GarminError("invalid_response", source=source)
    return float(value)


def _sleep_scores(item: dict) -> dict[str, int] | None:
    scores = item.get("sleepScores")
    if scores is None:
        return None
    if not isinstance(scores, dict) or len(scores) > 8:
        raise GarminError("invalid_response", source="sleep")
    result = {}
    for name, score in scores.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", name)
                or not isinstance(score, dict)):
            raise GarminError("invalid_response", source="sleep")
        value = _metric(score, "value", 0, 100, "sleep")
        if value is not None:
            result[name] = value
    return result or None


def _sleep_values(item: dict) -> dict:
    return {
        **{key: _metric(item, field, 0, 86400, "sleep") for key, field in _SLEEP_FIELDS.items()},
        "sleeping_respiration_brpm": _number_metric(item, "averageRespirationValue", 2**31 - 1, "sleep"),
        "sleep_scores": _sleep_scores(item),
    }


def _empty_sleep(item: dict) -> bool:
    """Only an explicit undated, null-duration placeholder may lack a date."""
    if "calendarDate" not in item or "sleepTimeSeconds" not in item:
        return False
    if item["calendarDate"] is not None or item["sleepTimeSeconds"] is not None:
        return False
    if any(value is not None for value in _sleep_values(item).values()):
        raise GarminError("invalid_response", source="sleep")
    return True


def _json(response: httpx.Response, source: str):
    try:
        return response.json()
    except (ValueError, UnicodeError):
        pass
    raise GarminError("invalid_response", source=source)


def _calendar_date(item: dict, source: str) -> date:
    value = item.get("calendarDate")
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise GarminError("invalid_response", source=source)


class GarminClient:
    """Explicit key only; fixed HTTPS host; injected httpx transport for tests."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not isinstance(api_key, str) or not api_key.strip() or not re.fullmatch(r"[!-~]+", api_key.strip()):
            raise GarminError("auth", source="auth")
        self._api_key = api_key.strip()
        self._token: str | None = None
        self._token_deadline = 0.0
        self._sleep = sleep
        self._closed = False
        self._http = httpx.Client(transport=transport, timeout=30.0, follow_redirects=False, trust_env=False)

    def __enter__(self) -> GarminClient:
        if self._closed:
            raise GarminError("closed")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True
        self._token = None
        self._api_key = ""
        self._token_deadline = 0.0
        self._http.close()

    def fetch_range(self, start: date, end: date) -> list[dict]:
        """Fetch an atomic, inclusive interval of 1..366 local calendar dates."""
        if self._closed:
            raise GarminError("closed")
        if type(start) is not date or type(end) is not date or not 0 <= (end - start).days < MAX_RANGE_DAYS:
            raise GarminError("invalid_range")
        days = {}
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            payload = self._get(SUMMARY_PATH, {"calendarDate": day.isoformat()}, "daily_summary")
            if not isinstance(payload, dict) or _calendar_date(payload, "daily_summary") != day:
                raise GarminError("invalid_response", source="daily_summary")
            # The schema also allows imported Fitbit records; don't label them
            # as Garmin measurements merely because Garmin served them.
            origin = payload.get("source")
            if origin is not None and (not isinstance(origin, str) or origin.upper() not in {"GARMIN", "FITBIT"}):
                raise GarminError("invalid_response", source="daily_summary")
            values = {key: _metric(payload, *spec, "daily_summary") for key, spec in _SUMMARY_FIELDS.items()}
            values["waking_respiration_brpm"] = _number_metric(payload, "avgWakingRespirationValue", 2**31 - 1, "daily_summary")
            if origin is not None and origin.upper() == "FITBIT":
                values = dict.fromkeys(values)
            days[day] = {
                "date": day.isoformat(), "provider": "garmin", **values, "sleep_seconds": None,
                **dict.fromkeys(_HRV_FIELDS), "hrv_status": None,
            }

            payload = self._get(SLEEP_DETAIL_PATH, {"date": day.isoformat()}, "sleep")
            if not isinstance(payload, dict):
                raise GarminError("invalid_response", source="sleep")
            if not _empty_sleep(payload):
                if _calendar_date(payload, "sleep") != day:
                    raise GarminError("invalid_response", source="sleep")
                # Validate even the scalar whose value comes from the range
                # source; malformed detail must not become a successful batch.
                _metric(payload, "sleepTimeSeconds", 0, 86400, "sleep")
            days[day].update(_sleep_values(payload))

            payload = self._get(SPO2_PATH.format(calendarDate=day.isoformat()), {}, "spo2")
            if not isinstance(payload, dict) or _calendar_date(payload, "spo2") != day:
                raise GarminError("invalid_response", source="spo2")
            days[day].update({
                "spo2_average_pct": _number_metric(payload, "averageSpO2", 100, "spo2"),
                "spo2_lowest_pct": _metric(payload, "lowestSpO2", 1, 100, "spo2"),
                "spo2_latest_pct": _metric(payload, "latestSpO2", 1, 100, "spo2"),
                "spo2_sleep_average_pct": _number_metric(payload, "avgSleepSpO2", 100, "spo2"),
            })

        chunk_start = start
        while True:
            chunk_end = chunk_start + timedelta(days=min(30, (end - chunk_start).days))
            payload = self._get(SLEEP_PATH, {"startDate": chunk_start.isoformat(), "endDate": chunk_end.isoformat()}, "sleep")
            if not isinstance(payload, list) or len(payload) > (chunk_end - chunk_start).days + 1:
                raise GarminError("invalid_response", source="sleep")
            seen = set()
            for item in payload:
                if not isinstance(item, dict):
                    raise GarminError("invalid_response", source="sleep")
                # Live Personal API returns an explicit undated/null placeholder
                # for a day whose sleep has not been finalized. It carries no
                # sleep duration; omit it without inventing a date or zero.
                if _empty_sleep(item):
                    continue
                day = _calendar_date(item, "sleep")
                if not chunk_start <= day <= chunk_end or day in seen:
                    raise GarminError("invalid_response", source="sleep")
                seen.add(day)
                days[day]["sleep_seconds"] = _metric(item, "sleepTimeSeconds", 0, 86400, "sleep")

            payload = self._get(HRV_PATH.format(fromDate=chunk_start.isoformat(), toDate=chunk_end.isoformat()), {}, "hrv")
            # The HRV range endpoint alone documents a 204 no-data response.
            # An actual JSON null (HTTP 200) is still malformed.
            if payload is not _NO_HRV_DATA:
                if not isinstance(payload, dict):
                    raise GarminError("invalid_response", source="hrv")
                summaries = payload.get("hrvSummaries")
                if not isinstance(summaries, list) or len(summaries) > (chunk_end - chunk_start).days + 1:
                    raise GarminError("invalid_response", source="hrv")
                seen = set()
                for item in summaries:
                    if not isinstance(item, dict):
                        raise GarminError("invalid_response", source="hrv")
                    day = _calendar_date(item, "hrv")
                    if not chunk_start <= day <= chunk_end or day in seen:
                        raise GarminError("invalid_response", source="hrv")
                    seen.add(day)
                    status = item.get("status")
                    if status is not None and (not isinstance(status, str) or status not in _HRV_STATUSES):
                        raise GarminError("invalid_response", source="hrv")
                    days[day].update({key: _metric(item, field, 1, 2**31 - 1, "hrv") for key, field in _HRV_FIELDS.items()})
                    days[day]["hrv_status"] = None if status in {"NONE", "INVALID"} else status
            if chunk_end == end:
                break
            chunk_start = chunk_end + timedelta(days=1)
        return list(days.values())

    def _authenticate(self) -> str:
        if self._token and time.monotonic() < self._token_deadline:
            return self._token
        self._token = None
        started = time.monotonic()
        response = self._request("POST", TOKEN_PATH, {"Authorization": f"ApiKey {self._api_key}"}, None, "auth")
        # Reference implementation returns raw text, not OAuth JSON. Decode exp
        # only for cache timing; server still verifies the JWT on every GET.
        token = response.text.strip()
        expiry = None
        if len(token) <= 16384 and re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            try:
                part = token.split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                expiry = claims.get("exp") if isinstance(claims, dict) else None
            except (ValueError, UnicodeError, binascii.Error):
                pass
        numeric_expiry = None
        if type(expiry) in (int, float):
            try:
                numeric_expiry = float(expiry)
            except OverflowError:
                pass
        if numeric_expiry is None or not math.isfinite(numeric_expiry):
            raise GarminError("invalid_response", source="auth")
        remaining = min(86400.0, numeric_expiry - time.time()) - TOKEN_MARGIN
        if remaining <= 0:
            raise GarminError("auth", source="auth")
        self._token = token
        self._token_deadline = started + remaining
        return token

    def _get(self, path: str, params: dict, source: str):
        for renewal in range(2):
            token = self._authenticate()
            try:
                response = self._request("GET", path, {"Authorization": f"Bearer {token}"}, params, source)
            except GarminError as error:
                if error.status_code != 401:
                    raise
                self._token = None
                self._token_deadline = 0.0
                if renewal:
                    raise
                continue
            if response.status_code == 204:
                return _NO_HRV_DATA
            return _json(response, source)
        raise GarminError("auth", source=source)

    def _request(self, method: str, path: str, headers: dict, params: dict | None, source: str) -> httpx.Response:
        # No caller-selectable origin, arbitrary GET, write operation, redirects,
        # proxy environment, or cookies from another host.
        if ((method, path) not in {("POST", TOKEN_PATH), ("GET", SUMMARY_PATH), ("GET", SLEEP_PATH), ("GET", SLEEP_DETAIL_PATH)}
                and not (method == "GET" and _DATED_GET_PATH.fullmatch(path))):
            raise GarminError("http", source=source)
        attempts = MAX_ATTEMPTS if method == "GET" else 1
        for attempt in range(attempts):
            response = None
            try:
                response = self._http.request(method, BASE_URL + path, headers={"Accept": "application/json" if method == "GET" else "*/*", **headers}, params=params)
            except httpx.HTTPError:
                pass
            # Raise outside the except block so SDK errors carrying credentials
            # are not attached as even a suppressed exception context.
            if response is None:
                if attempt + 1 == attempts:
                    raise GarminError("unavailable", source=source)
                self._sleep(0.5 * 2**attempt)
                continue
            status = response.status_code
            if status == 200:
                return response
            if status == 204 and method == "GET" and path.startswith("/hrv-service/hrv/daily/"):
                return response
            retryable = status == 429 or 500 <= status <= 599
            if retryable and attempt + 1 < attempts:
                self._sleep(self._retry_delay(response, attempt))
                continue
            kind = "auth" if status in {401, 403} else "rate_limit" if status == 429 else "unavailable" if status >= 500 else "http"
            raise GarminError(kind, status_code=status, source=source)
        raise GarminError("unavailable", source=source)

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After", "")
        delay = None
        try:
            delay = float(header)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(header)
                if retry_at.tzinfo is not None:
                    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
        if delay is not None and math.isfinite(delay) and delay >= 0:
            return min(MAX_RETRY_AFTER, delay)
        return min(8.0, 0.5 * 2**attempt)
