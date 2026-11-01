"""Official OpenAPI field/endpoint contracts, using only synthetic transports."""

import json
import re
import traceback
from datetime import date, timedelta

import httpx
import pytest

import app.garmin.client as module
from app.garmin import GarminError
from test_garmin_client import DAY, KEY, clock, empty_day, jwt, make_client


SPO2_DAY_PATH = module.SPO2_PATH.format(calendarDate=DAY.isoformat())
HRV_DAY_PATH = module.HRV_PATH.format(fromDate=DAY.isoformat(), toDate=DAY.isoformat())
NEW_SOURCES = [(module.SLEEP_DETAIL_PATH, "sleep"), (SPO2_DAY_PATH, "spo2"), (HRV_DAY_PATH, "hrv")]


def test_actual_client_empty_contract_normalizes_without_fabricated_scores():
    from app.garmin_data import normalize_days
    with make_client()[0] as client:
        normalized = normalize_days(client.fetch_range(DAY, DAY), DAY, DAY)
    assert normalized[0]["sleep_scores"] == {}
    assert normalized[0]["spo2_average_pct"] is None
    assert normalized[0]["hrv_status"] is None


def response_for(path, payload):
    return lambda request: httpx.Response(200, json=payload) if request.url.path == path else None


def test_exact_official_fields_flat_contract_and_no_samples_or_narrative():
    def handler(request):
        path = request.url.path
        if path == module.SUMMARY_PATH:
            return httpx.Response(200, json={
                "calendarDate": DAY.isoformat(), "source": "GARMIN",
                "totalSteps": 5000, "restingHeartRate": 55, "averageStressLevel": 24,
                "bodyBatteryHighestValue": 90, "bodyBatteryLowestValue": 20,
                "bodyBatteryAtWakeTime": 81, "bodyBatteryMostRecentValue": 35,
                "avgWakingRespirationValue": 14.25,
                "averageSpo2": 1, "lowestSpo2": 1, "latestSpo2": 1,
                "bodyBatteryDuringSleep": 44, "wellnessDescription": KEY,
            })
        if path == module.SLEEP_PATH:
            return httpx.Response(200, json=[{"calendarDate": DAY.isoformat(), "sleepTimeSeconds": 25200}])
        if path == module.SLEEP_DETAIL_PATH:
            return httpx.Response(200, json={
                "calendarDate": DAY.isoformat(), "sleepTimeSeconds": 25200,
                "deepSleepSeconds": 3600, "lightSleepSeconds": 14400,
                "remSleepSeconds": 7200, "awakeSleepSeconds": 600, "napTimeSeconds": 1200,
                "averageRespirationValue": 12.75,
                "sleepScores": {
                    "duration": {"value": 81, "qualifierKey": KEY, "optimalStart": 5.0},
                    "REM-score": {"value": 0}, "unavailable": {"value": -1},
                },
                "averageSpO2Value": 1.0, "averageSpO2HRSleep": 2.0,
                "userNote": KEY, "dailyNapDTOS": [{"note": KEY}],
                "sleepStartTimestampGMT": "2026-10-31T22:00:00Z",
            })
        if path == SPO2_DAY_PATH:
            return httpx.Response(200, json={
                "calendarDate": DAY.isoformat(), "averageSpO2": 96.75,
                "lowestSpO2": 91, "latestSpO2": 98, "avgSleepSpO2": 95.125,
                "avgTomorrowSleepSpO2": 10, "lastSevenDaysAvgSpO2": 20,
                "spO2SingleValues": [[KEY]], "spO2HourlyAverages": [[KEY]],
                "monitoringEnvironmentValues": [[KEY]], "userProfilePK": 12345,
            })
        if path == HRV_DAY_PATH:
            return httpx.Response(200, json={"userProfilePk": 12345, "hrvSummaries": [{
                "calendarDate": DAY.isoformat(), "lastNightAvg": 62,
                "weeklyAvg": 58, "lastNight5MinHigh": 90, "status": "BALANCED",
                "feedbackPhrase": KEY, "baseline": {"narrative": KEY},
                "createTimeStamp": "2026-11-02T01:00:00", "hrvReadings": [[KEY]],
            }]})

    client, requests = make_client(handler)
    with client:
        found = client.fetch_range(DAY, DAY)
    assert found == [{
        **empty_day(), "steps": 5000, "resting_hr_bpm": 55, "average_stress_level": 24,
        "body_battery_highest": 90, "body_battery_lowest": 20,
        "body_battery_at_wake": 81, "body_battery_most_recent": 35,
        "waking_respiration_brpm": 14.25, "sleeping_respiration_brpm": 12.75,
        "sleep_seconds": 25200, "deep_sleep_seconds": 3600, "light_sleep_seconds": 14400,
        "rem_sleep_seconds": 7200, "awake_sleep_seconds": 600, "nap_sleep_seconds": 1200,
        "sleep_scores": {"duration": 81, "REM-score": 0},
        "spo2_average_pct": 96.75, "spo2_lowest_pct": 91, "spo2_latest_pct": 98,
        "spo2_sleep_average_pct": 95.125, "hrv_last_night_ms": 62,
        "hrv_weekly_avg_ms": 58, "hrv_last_night_5min_high_ms": 90, "hrv_status": "BALANCED",
    }]
    assert KEY not in json.dumps(found)
    assert jwt() not in json.dumps(found)
    assert dict(requests[2].url.params) == {"date": DAY.isoformat()}
    assert [r.url.path for r in requests] == [
        module.TOKEN_PATH, module.SUMMARY_PATH, module.SLEEP_DETAIL_PATH,
        SPO2_DAY_PATH, module.SLEEP_PATH, HRV_DAY_PATH,
    ]
    for name in ("waking_respiration_brpm", "sleeping_respiration_brpm", "spo2_average_pct", "spo2_sleep_average_pct"):
        assert type(found[0][name]) is float


@pytest.mark.parametrize("status", [None, "NONE", "INVALID", "POOR", "LOW", "UNBALANCED", "BALANCED"])
def test_only_documented_hrv_statuses_and_missing_sentinels(status):
    payload = {"hrvSummaries": [{"calendarDate": DAY.isoformat(), "status": status}]}
    with make_client(response_for(HRV_DAY_PATH, payload))[0] as client:
        assert client.fetch_range(DAY, DAY)[0]["hrv_status"] == (None if status in {"NONE", "INVALID"} else status)


@pytest.mark.parametrize("status", ["balanced", "HIGH", "", KEY, 1, True, [], {}])
def test_hrv_status_unknown_or_non_string_is_safe_failure(status):
    payload = {"hrvSummaries": [{"calendarDate": DAY.isoformat(), "status": status}]}
    with make_client(response_for(HRV_DAY_PATH, payload))[0] as client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert caught.value.kind == "invalid_response"
    assert caught.value.source == "hrv"
    assert KEY not in str(caught.value)


NUMBER_FIELDS = [
    (module.SUMMARY_PATH, "avgWakingRespirationValue", "waking_respiration_brpm"),
    (module.SLEEP_DETAIL_PATH, "averageRespirationValue", "sleeping_respiration_brpm"),
    (SPO2_DAY_PATH, "averageSpO2", "spo2_average_pct"),
    (SPO2_DAY_PATH, "avgSleepSpO2", "spo2_sleep_average_pct"),
]
INTEGER_FIELDS = [
    (module.SUMMARY_PATH, "bodyBatteryAtWakeTime", "body_battery_at_wake"),
    (module.SUMMARY_PATH, "bodyBatteryMostRecentValue", "body_battery_most_recent"),
    *[(module.SLEEP_DETAIL_PATH, field, key) for key, field in module._SLEEP_FIELDS.items()],
    (SPO2_DAY_PATH, "lowestSpO2", "spo2_lowest_pct"),
    (SPO2_DAY_PATH, "latestSpO2", "spo2_latest_pct"),
    *[(HRV_DAY_PATH, field, key) for key, field in module._HRV_FIELDS.items()],
]


def metric_payload(path, field, value):
    item = {"calendarDate": DAY.isoformat(), field: value}
    return {"hrvSummaries": [item]} if path == HRV_DAY_PATH else item


@pytest.mark.parametrize("path,field,key", NUMBER_FIELDS)
@pytest.mark.parametrize("value,expected", [(None, None), (-1, None), (-0.5, None), (0, None), (1, 1.0), (16.375, 16.375)])
def test_numeric_schema_retains_floats_and_nulls(path, field, key, value, expected):
    with make_client(response_for(path, metric_payload(path, field, value)))[0] as client:
        result = client.fetch_range(DAY, DAY)[0][key]
    assert result == expected
    if expected is not None:
        assert type(result) is float


@pytest.mark.parametrize("path,field,key", NUMBER_FIELDS)
@pytest.mark.parametrize("value", [True, "16.5", [], {}, 2**2048, float("nan"), float("inf"), float("-inf")])
def test_malformed_nonfinite_numeric_schema_fields_fail(path, field, key, value):
    # Python's encoder emits non-standard NaN/Infinity, deliberately exercising
    # a decoder that accepts them. No exception may carry the input body.
    body = json.dumps(metric_payload(path, field, value))
    def handler(request):
        if request.url.path == path:
            return httpx.Response(200, text=body)
    with make_client(handler)[0] as client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert caught.value.kind == "invalid_response"
    assert caught.value.__context__ is None


@pytest.mark.parametrize("path,field,key", INTEGER_FIELDS)
@pytest.mark.parametrize("value", [True, "30", 30.5, {}, 2**31])
def test_schema_integers_do_not_coerce_or_overflow(path, field, key, value):
    with make_client(response_for(path, metric_payload(path, field, value)))[0] as client, pytest.raises(GarminError):
        client.fetch_range(DAY, DAY)


@pytest.mark.parametrize("path,field,key", INTEGER_FIELDS)
@pytest.mark.parametrize("value", [None, -1, 0])
def test_integer_sentinels_and_meaningful_zero(path, field, key, value):
    with make_client(response_for(path, metric_payload(path, field, value)))[0] as client:
        found = client.fetch_range(DAY, DAY)[0][key]
    zero_is_value = path in {module.SUMMARY_PATH, module.SLEEP_DETAIL_PATH}
    assert found == (0 if zero_is_value and value == 0 else None)


@pytest.mark.parametrize("field", ["averageSpO2", "lowestSpO2", "latestSpO2", "avgSleepSpO2"])
def test_spo2_percentages_never_exceed_100(field):
    with make_client(response_for(SPO2_DAY_PATH, metric_payload(SPO2_DAY_PATH, field, 101)))[0] as client, pytest.raises(GarminError):
        client.fetch_range(DAY, DAY)


@pytest.mark.parametrize("field", ["bodyBatteryAtWakeTime", "bodyBatteryMostRecentValue"])
def test_new_body_battery_values_remain_percent_bounded(field):
    with make_client(response_for(module.SUMMARY_PATH, metric_payload(module.SUMMARY_PATH, field, 101)))[0] as client, pytest.raises(GarminError):
        client.fetch_range(DAY, DAY)


@pytest.mark.parametrize("field", ["sleepTimeSeconds", "deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds", "awakeSleepSeconds", "napTimeSeconds"])
def test_every_detailed_sleep_duration_is_validated_even_when_total_comes_from_summaries(field):
    payload = {"calendarDate": DAY.isoformat(), field: 86401}
    with make_client(response_for(module.SLEEP_DETAIL_PATH, payload))[0] as client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert caught.value.source == "sleep"


def test_score_names_are_dynamic_with_exact_parent_pattern_and_eight_bound():
    names = ["A", "a" * 40, "REM-score", "deep_sleep", "x5", "stage", "rest", "duration"]
    scores = {name: {"value": n, "qualifierKey": KEY} for n, name in enumerate(names)}
    payload = {"calendarDate": DAY.isoformat(), "sleepScores": scores}
    with make_client(response_for(module.SLEEP_DETAIL_PATH, payload))[0] as client:
        found = client.fetch_range(DAY, DAY)[0]["sleep_scores"]
    assert found == dict(zip(names, range(8)))
    assert "overall" not in found
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", key) for key in found)


@pytest.mark.parametrize("scores", [
    [], "secret", {"_bad": {"value": 1}}, {"5bad": {"value": 1}},
    {"a" * 41: {"value": 1}}, {"a.b": {"value": 1}}, {"é": {"value": 1}},
    {"a\n": {"value": 1}}, {"note text": {"value": 1}},
    {"score": 10}, {"score": None}, {"score": {"value": True}},
    {"score": {"value": "80"}}, {"score": {"value": 80.5}}, {"score": {"value": 101}},
    {f"score{i}": {"value": i} for i in range(9)},
])
def test_invalid_sleep_scores_fail_entire_batch(scores):
    payload = {"calendarDate": DAY.isoformat(), "sleepScores": scores}
    with make_client(response_for(module.SLEEP_DETAIL_PATH, payload))[0] as client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert caught.value.kind == "invalid_response"
    assert caught.value.source == "sleep"


@pytest.mark.parametrize("scores", [None, {}, {"a": {}}, {"a": {"value": None}}, {"a": {"value": -1}}])
def test_absent_or_sentinel_scores_are_none(scores):
    payload = {"calendarDate": DAY.isoformat(), "sleepScores": scores}
    with make_client(response_for(module.SLEEP_DETAIL_PATH, payload))[0] as client:
        assert client.fetch_range(DAY, DAY)[0]["sleep_scores"] is None


@pytest.mark.parametrize("path,source", NEW_SOURCES)
@pytest.mark.parametrize("payload", [None, [], {}, {"calendarDate": "2026-11-02"}, {"calendarDate": "2026-02-30"}, {"calendarDate": DAY.isoformat() + "T00:00:00Z"}])
def test_extended_wrong_shapes_and_dates_fail_before_returning_days(path, source, payload):
    if source == "hrv" and isinstance(payload, dict) and "calendarDate" in payload:
        payload = {"hrvSummaries": [payload]}
    with make_client(response_for(path, payload))[0] as client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert caught.value.kind == "invalid_response"
    assert caught.value.source == source


@pytest.mark.parametrize("payload", [
    {"hrvSummaries": None}, {"hrvSummaries": {}}, {"hrvSummaries": [None]},
    {"hrvSummaries": [{}]}, {"hrvSummaries": [{"calendarDate": None}]},
    {"hrvSummaries": [{"calendarDate": DAY.isoformat()}] * 2},
])
def test_hrv_malformed_and_oversized_lists_rejected(payload):
    with make_client(response_for(HRV_DAY_PATH, payload))[0] as client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert caught.value.source == "hrv"


def test_hrv_unsorted_sparse_dates_and_duplicate_detection():
    end = DAY + timedelta(days=2)
    path = module.HRV_PATH.format(fromDate=DAY.isoformat(), toDate=end.isoformat())
    items = [{"calendarDate": end.isoformat(), "lastNightAvg": 70},
             {"calendarDate": DAY.isoformat(), "lastNightAvg": 60}]
    with make_client(response_for(path, {"hrvSummaries": items}))[0] as client:
        assert [d["hrv_last_night_ms"] for d in client.fetch_range(DAY, end)] == [60, None, 70]
    items.append(dict(items[0]))
    with make_client(response_for(path, {"hrvSummaries": items}))[0] as client, pytest.raises(GarminError):
        client.fetch_range(DAY, end)


@pytest.mark.parametrize("count", [1, 7, 31, 32, 366])
def test_all_new_requests_bounded_and_hrv_chunks_inclusive(count):
    start = date(2024, 1, 1)
    end = start + timedelta(days=count - 1)
    client, requests = make_client()
    with client:
        assert len(client.fetch_range(start, end)) == count
    assert len(requests) == 1 + 3 * count + 2 * ((count + 30) // 31)
    covered = []
    for request in requests:
        if request.url.path.startswith("/hrv-service/hrv/daily/"):
            first, last = map(date.fromisoformat, request.url.path.split("/")[-2:])
            assert 0 <= (last - first).days <= 30
            covered.extend(first + timedelta(days=n) for n in range((last - first).days + 1))
    assert covered == [start + timedelta(days=n) for n in range(count)]


@pytest.mark.parametrize("path,source", NEW_SOURCES)
@pytest.mark.parametrize("status", [204, 404, 403, 429, 503, 302])
def test_extended_http_failures_are_safe_and_only_hrv_204_is_missing(path, source, status, caplog):
    def handler(request):
        if request.url.path == path:
            return httpx.Response(status, headers={"Location": "https://other.invalid/"}, text=KEY)
    client, requests = make_client(handler)
    with client:
        if source == "hrv" and status == 204:
            assert client.fetch_range(DAY, DAY) == [empty_day()]
        else:
            with pytest.raises(GarminError) as caught:
                client.fetch_range(DAY, DAY)
            assert caught.value.source == source
            assert caught.value.status_code == (status if status in module._STATUSES else None)
            assert caught.value.__context__ is None
            assert KEY not in "".join(traceback.format_exception(caught.value)) + caplog.text
    assert sum(r.url.path == path for r in requests) == (3 if status in {429, 503} else 1)
    assert all(r.url.host == "personal.connectapi.garmin.com" for r in requests)


@pytest.mark.parametrize("path,source", NEW_SOURCES)
@pytest.mark.parametrize("persistent", [False, True])
def test_new_endpoints_renew_only_once_on_401(path, source, persistent):
    calls = 0
    def handler(request):
        nonlocal calls
        if request.url.path == path:
            calls += 1
            if persistent or calls == 1:
                return httpx.Response(401, text=KEY)
    client, requests = make_client(handler)
    with client:
        if persistent:
            with pytest.raises(GarminError) as caught:
                client.fetch_range(DAY, DAY)
            assert caught.value.source == source
            assert client._token is None
        else:
            assert client.fetch_range(DAY, DAY) == [empty_day()]
    assert calls == 2
    assert sum(r.method == "POST" for r in requests) == 2


@pytest.mark.parametrize("path,source", NEW_SOURCES)
def test_new_endpoints_network_failures_and_invalid_json_hide_raw_errors(path, source):
    for network in (False, True):
        def handler(request):
            if request.url.path == path:
                if network:
                    raise httpx.ReadTimeout(KEY, request=request)
                return httpx.Response(200, text=KEY)
        with make_client(handler)[0] as client, pytest.raises(GarminError) as caught:
            client.fetch_range(DAY, DAY)
        assert caught.value.kind == ("unavailable" if network else "invalid_response")
        assert caught.value.source == source
        assert caught.value.__context__ is None
        assert caught.value.__cause__ is None


def test_second_hrv_chunk_failure_discards_entire_batch():
    def handler(request):
        if request.url.path.startswith("/hrv-service/hrv/daily/"):
            first = request.url.path.split("/")[-2]
            if first != DAY.isoformat():
                return httpx.Response(404, text=KEY)
            return httpx.Response(200, json={"hrvSummaries": [{"calendarDate": DAY.isoformat(), "lastNightAvg": 60}]})
    with make_client(handler)[0] as client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY + timedelta(days=31))
    assert caught.value.source == "hrv"
    assert caught.value.status_code == 404


def test_undated_empty_detail_is_missing_but_metrics_cannot_be_attached_to_guessed_date():
    payload = {"calendarDate": None, "sleepTimeSeconds": None}
    with make_client(response_for(module.SLEEP_DETAIL_PATH, payload))[0] as client:
        assert client.fetch_range(DAY, DAY) == [empty_day()]
    for field, value in [("deepSleepSeconds", 0), ("napTimeSeconds", 60),
                         ("sleepScores", {"deep": {"value": 50}}), ("averageRespirationValue", 12.5)]:
        with make_client(response_for(module.SLEEP_DETAIL_PATH, {**payload, field: value}))[0] as client, pytest.raises(GarminError):
            client.fetch_range(DAY, DAY)


def test_fitbit_origin_suppresses_new_summary_metrics_but_not_separate_garmin_sources():
    def handler(request):
        if request.url.path == module.SUMMARY_PATH:
            return httpx.Response(200, json={"calendarDate": DAY.isoformat(), "source": "FITBIT",
                "bodyBatteryAtWakeTime": 80, "bodyBatteryMostRecentValue": 40, "avgWakingRespirationValue": 15.25})
        if request.url.path == SPO2_DAY_PATH:
            return httpx.Response(200, json={"calendarDate": DAY.isoformat(), "averageSpO2": 97.5})
    with make_client(handler)[0] as client:
        assert client.fetch_range(DAY, DAY) == [{**empty_day(), "spo2_average_pct": 97.5}]


@pytest.mark.parametrize("path", [
    "/hrv-service/hrv/2026-11-01", "/activitylist-service/activities/search/activities",
    "/metrics-service/metrics/trainingreadiness/2026-11-01", SPO2_DAY_PATH + "/extra",
    SPO2_DAY_PATH + "?origin=https://other.invalid/", "https://other.invalid" + SPO2_DAY_PATH,
    "/hrv-service/hrv/daily/2026-11-01/2026-11-01/extra",
])
def test_no_arbitrary_sample_activity_or_training_readiness_requests(path):
    client, requests = make_client()
    with client, pytest.raises(GarminError):
        client._request("GET", path, {}, {}, "hrv")
    assert requests == []
