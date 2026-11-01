"""Synthetic payloads from the official DTOs; no keys, vaults or live API calls."""

import base64
import json
import traceback
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

import app.garmin.client as module
from app.garmin import GarminClient, GarminError

KEY = "synthetic-key-never-real"
DAY = date(2026, 11, 1)
NOW = 1_793_491_200.0


def jwt(exp=NOW + 86400, **extra):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'RS256'})}.{encode({'exp': exp, **extra})}.synthetic_signature"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    current = SimpleNamespace(wall=NOW, monotonic=1000.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: current.wall, monotonic=lambda: current.monotonic))
    return current


def empty_day(day=DAY):
    return {
        "date": day.isoformat(), "provider": "garmin", "steps": None,
        "sleep_seconds": None, "resting_hr_bpm": None,
        "average_stress_level": None, "body_battery_highest": None,
        "body_battery_lowest": None,
        "body_battery_at_wake": None, "body_battery_most_recent": None,
        "waking_respiration_brpm": None, "sleeping_respiration_brpm": None,
        "spo2_average_pct": None, "spo2_lowest_pct": None,
        "spo2_latest_pct": None, "spo2_sleep_average_pct": None,
        "hrv_last_night_ms": None, "hrv_weekly_avg_ms": None,
        "hrv_last_night_5min_high_ms": None, "hrv_status": None,
        "sleep_scores": None, "deep_sleep_seconds": None,
        "light_sleep_seconds": None, "rem_sleep_seconds": None,
        "awake_sleep_seconds": None, "nap_sleep_seconds": None,
    }


def make_client(callback=None, *, delays=None):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "personal.connectapi.garmin.com"
        assert request.method in {"GET", "POST"}
        assert not request.content
        if callback:
            response = callback(request)
            if response is not None:
                return response
        if request.url.path == module.TOKEN_PATH:
            assert request.method == "POST"
            assert request.headers["Authorization"] == f"ApiKey {KEY}"
            return httpx.Response(200, text=jwt())
        assert request.method == "GET"
        assert request.headers["Authorization"].startswith("Bearer ")
        assert KEY not in str(request.url)
        if request.url.path == module.SUMMARY_PATH:
            assert set(request.url.params) == {"calendarDate"}
            return httpx.Response(200, json={"calendarDate": request.url.params["calendarDate"]})
        if request.url.path == module.SLEEP_DETAIL_PATH:
            assert set(request.url.params) == {"date"}
            return httpx.Response(200, json={"calendarDate": request.url.params["date"]})
        if request.url.path.startswith("/wellness-service/wellness/daily/spo2acclimation/"):
            assert not request.url.params
            return httpx.Response(200, json={"calendarDate": request.url.path.rsplit("/", 1)[1]})
        if request.url.path.startswith("/hrv-service/hrv/daily/"):
            assert not request.url.params
            return httpx.Response(200, json={"hrvSummaries": []})
        assert request.url.path == module.SLEEP_PATH
        assert set(request.url.params) == {"startDate", "endDate"}
        return httpx.Response(200, json=[])

    return GarminClient(KEY, transport=httpx.MockTransport(handler), sleep=(delays if delays is not None else []).append), requests


def test_official_plain_jwt_auth_and_memory_cache(clock):
    with make_client()[0] as client:
        assert client.fetch_range(DAY, DAY) == [empty_day()]
        token = client._token
        assert token == jwt()
        assert client.fetch_range(DAY, DAY) == [empty_day()]
        clock.wall += 86339
        clock.monotonic += 86339
        assert client.fetch_range(DAY, DAY) == [empty_day()]
        assert client._token == token


def test_auth_is_lazy_and_cached_once_per_client():
    client, requests = make_client()
    with client:
        assert requests == []
        client.fetch_range(DAY, DAY)
        client.fetch_range(DAY, DAY)
        assert [r.method for r in requests] == ["POST"] + ["GET"] * 10
        assert KEY not in repr(client)
        assert jwt() not in repr(client)
    assert client._token is None
    assert client._api_key == ""
    assert client._http.is_closed
    client.close()
    with pytest.raises(GarminError, match="closed"):
        client.fetch_range(DAY, DAY)
    with pytest.raises(GarminError, match="closed"):
        client.__enter__()


def test_expiry_margin_renews_with_no_refresh_endpoint(clock):
    client, requests = make_client(
        lambda r: httpx.Response(200, text=jwt(clock.wall + 86400))
        if r.method == "POST" else None
    )
    with client:
        client.fetch_range(DAY, DAY)
        clock.wall += 86340
        clock.monotonic += 86340
        client.fetch_range(DAY, DAY)
    assert sum(r.method == "POST" for r in requests) == 2
    assert {r.url.path for r in requests if r.method == "POST"} == {module.TOKEN_PATH}


def test_cache_never_exceeds_24_hours_even_with_distant_exp(clock):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, text=jwt(NOW + 86400 * 50))

    client, requests = make_client(handler)
    with client:
        client.fetch_range(DAY, DAY)
        clock.monotonic += 86340
        clock.wall += 86340
        client.fetch_range(DAY, DAY)
    assert sum(r.method == "POST" for r in requests) == 2


@pytest.mark.parametrize("body", ["", "not-a-jwt", json.dumps({"access_token": jwt()}), "a.e30.c", jwt(None), jwt(True), jwt("secret"), jwt(float("nan")), jwt(float("inf")), jwt(2**2048), "a.____.c"])
def test_malformed_token_fails_safely(body):
    client, requests = make_client(lambda r: httpx.Response(200, text=body))
    with client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert caught.value.kind == "invalid_response"
    assert caught.value.source == "auth"
    assert caught.value.__context__ is None
    assert len(requests) == 1
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("exp", [NOW - 1, NOW, NOW + 59, NOW + 60])
def test_expired_or_near_expiry_tokens_are_not_used(exp):
    client, requests = make_client(lambda r: httpx.Response(200, text=jwt(exp)))
    with client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert caught.value.kind == "auth"
    assert len(requests) == 1


@pytest.mark.parametrize("key", [None, "", " \n ", 123, "secret\r\nInjected: value", "secret\u2603", "secret key"])
def test_invalid_keys_are_rejected_before_http(key):
    with pytest.raises(GarminError) as caught:
        GarminClient(key)
    assert caught.value.kind == "auth"


def test_documented_metrics_only_and_missing_values_stay_missing():
    def handler(request):
        if request.url.path == module.SUMMARY_PATH:
            return httpx.Response(200, json={
                "calendarDate": DAY.isoformat(), "source": "GARMIN", "totalSteps": 4321,
                "restingHeartRate": 57, "minHeartRate": 40, "averageStressLevel": 26,
                "bodyBatteryHighestValue": 91, "bodyBatteryLowestValue": 17,
                "displayName": KEY, "access_token": jwt(), "gps": [[1, 2]],
                "heartRateValues": [[NOW, 60]], "sleepingSeconds": 999,
            })
        if request.url.path == module.SLEEP_PATH:
            return httpx.Response(200, json=[{
                "calendarDate": DAY.isoformat(), "sleepTimeSeconds": 25200,
                "napTimeSeconds": 1200, "userNote": KEY, "sleepScores": {"overall": {"value": 80}},
            }])

    with make_client(handler)[0] as client:
        found = client.fetch_range(DAY, DAY)
    assert found == [{**empty_day(), "steps": 4321, "sleep_seconds": 25200, "resting_hr_bpm": 57,
                      "average_stress_level": 26, "body_battery_highest": 91, "body_battery_lowest": 17}]
    assert KEY not in json.dumps(found)
    assert jwt() not in json.dumps(found)


@pytest.mark.parametrize("value,expected", [(None, None), (-1, None), (0, 0)])
def test_missing_sentinels_and_actual_zero_are_distinct(value, expected):
    def handler(request):
        if request.url.path == module.SUMMARY_PATH:
            return httpx.Response(200, json={"calendarDate": DAY.isoformat(), "totalSteps": value, "restingHeartRate": value})
        if request.url.path == module.SLEEP_PATH:
            return httpx.Response(200, json=[{"calendarDate": DAY.isoformat(), "sleepTimeSeconds": value}])

    with make_client(handler)[0] as client:
        day = client.fetch_range(DAY, DAY)[0]
    assert day["steps"] == expected
    assert day["sleep_seconds"] == expected
    assert day["resting_hr_bpm"] is None


def test_fitbit_import_is_not_mislabeled_as_garmin_measurements():
    def handler(request):
        if request.url.path == module.SUMMARY_PATH:
            return httpx.Response(200, json={"calendarDate": DAY.isoformat(), "source": "FITBIT", "totalSteps": 100, "restingHeartRate": 50})

    with make_client(handler)[0] as client:
        assert client.fetch_range(DAY, DAY) == [empty_day()]


@pytest.mark.parametrize("path", [module.SUMMARY_PATH, module.SLEEP_PATH])
@pytest.mark.parametrize("always_unauthorized", [False, True])
def test_401_renews_once_per_get(path, always_unauthorized):
    calls = 0
    tokens = 0

    def handler(request):
        nonlocal calls, tokens
        if request.method == "POST":
            tokens += 1
            return httpx.Response(200, text=jwt(generation=tokens))
        if request.url.path == path:
            calls += 1
            assert request.headers["Authorization"] == f"Bearer {jwt(generation=tokens)}"
            if always_unauthorized or calls == 1:
                return httpx.Response(401, text=KEY)

    client, requests = make_client(handler)
    with client:
        if always_unauthorized:
            with pytest.raises(GarminError) as caught:
                client.fetch_range(DAY, DAY)
            assert caught.value.kind == "auth"
            assert caught.value.status_code == 401
            assert client._token is None
        else:
            assert client.fetch_range(DAY, DAY) == [empty_day()]
    assert tokens == 2
    assert calls == 2


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 599])
def test_get_retries_transient_errors_with_bounded_backoff(status):
    failures = 0
    delays = []

    def handler(request):
        nonlocal failures
        if request.url.path == module.SUMMARY_PATH and failures < 2:
            failures += 1
            return httpx.Response(status, text=KEY)

    with make_client(handler, delays=delays)[0] as client:
        assert client.fetch_range(DAY, DAY) == [empty_day()]
    assert delays == [0.5, 1.0]


@pytest.mark.parametrize("header,expected", [("99999999", 30), ("1.5", 1.5), ("0", 0), ("-2", 0.5), ("NaN", 0.5), ("inf", 0.5), ("nonsense", 0.5)])
def test_retry_after_is_capped_and_invalid_values_fall_back(header, expected):
    failures = 0
    delays = []

    def handler(request):
        nonlocal failures
        if request.url.path == module.SLEEP_PATH and failures == 0:
            failures += 1
            return httpx.Response(429, headers={"Retry-After": header}, text=KEY)

    with make_client(handler, delays=delays)[0] as client:
        client.fetch_range(DAY, DAY)
    assert delays == [expected]


def test_retry_after_http_date_is_capped():
    header = format_datetime(datetime.now(timezone.utc) + timedelta(days=1), usegmt=True)
    assert module.GarminClient._retry_delay(httpx.Response(429, headers={"Retry-After": header}), 0) == 30


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("failure", ["network", "status"])
def test_exhaustion_and_auth_never_retry_post_or_expose_secrets(method, failure, caplog):
    delays = []

    def handler(request):
        if request.method == method:
            if failure == "network":
                raise httpx.ConnectError(f"{KEY} {jwt()} private response", request=request)
            return httpx.Response(503, text=f"{KEY} {jwt()} private response")

    client, requests = make_client(handler, delays=delays)
    with client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert caught.value.kind == "unavailable"
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    rendered = "".join(traceback.format_exception(caught.value)) + repr(vars(caught.value)) + caplog.text
    assert KEY not in rendered
    assert jwt() not in rendered
    assert "private response" not in rendered
    assert sum(r.method == method for r in requests) == (3 if method == "GET" else 1)
    assert delays == ([0.5, 1.0] if method == "GET" else [])


def test_network_get_recovers():
    failures = 0
    delays = []

    def handler(request):
        nonlocal failures
        if request.url.path == module.SUMMARY_PATH and failures < 2:
            failures += 1
            raise httpx.ReadTimeout(KEY, request=request)

    with make_client(handler, delays=delays)[0] as client:
        assert client.fetch_range(DAY, DAY) == [empty_day()]
    assert delays == [0.5, 1.0]


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_token_http_errors_are_not_retried(status):
    delays = []
    client, requests = make_client(lambda r: httpx.Response(status, text=KEY), delays=delays)
    with client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert caught.value.source == "auth"
    assert caught.value.status_code == status
    assert delays == []


@pytest.mark.parametrize("path", [module.TOKEN_PATH, module.SUMMARY_PATH, module.SLEEP_PATH])
@pytest.mark.parametrize("status", [301, 302, 307, 308])
@pytest.mark.parametrize("location", ["https://credential-thief.invalid/", module.BASE_URL + "/redirected", "http://personal.connectapi.garmin.com/"])
def test_redirects_are_never_followed_even_to_same_host(path, status, location):
    def handler(request):
        if request.url.path == path:
            return httpx.Response(status, headers={"Location": location}, text=KEY)

    client, requests = make_client(handler)
    with client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert caught.value.kind == "http"
    assert all(r.url.host == "personal.connectapi.garmin.com" for r in requests)
    assert sum(r.url.path == path for r in requests) == 1


@pytest.mark.parametrize("status,kind", [(400, "http"), (403, "auth"), (404, "http"), (204, "http"), (418, "http"), (429, "rate_limit")])
def test_http_errors_do_not_become_missing_data(status, kind):
    client, requests = make_client(lambda r: httpx.Response(status, text=KEY) if r.url.path == module.SLEEP_PATH else None)
    with client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY)
    assert caught.value.kind == kind
    assert caught.value.source == "sleep"
    assert caught.value.status_code == (status if status in module._STATUSES else None)
    assert sum(r.url.path == module.SLEEP_PATH for r in requests) == (3 if status == 429 else 1)


@pytest.mark.parametrize("payload", [None, [], {}, {"calendarDate": "2026-10-31"}, {"calendarDate": "2026-02-30"}, {"calendarDate": "20261101"}, {"calendarDate": DAY.isoformat(), "source": "invented"}])
def test_malformed_summary_shapes_dates_and_source(payload):
    with make_client(lambda r: httpx.Response(200, json=payload) if r.url.path == module.SUMMARY_PATH else None)[0] as client:
        with pytest.raises(GarminError) as caught:
            client.fetch_range(DAY, DAY)
    assert caught.value.kind == "invalid_response"
    assert caught.value.source == "daily_summary"


@pytest.mark.parametrize("field,value", [("totalSteps", True), ("restingHeartRate", "57"), ("restingHeartRate", 57.5), ("totalSteps", {}), ("totalSteps", 2**31), ("averageStressLevel", 101), ("bodyBatteryHighestValue", 101)])
def test_malformed_summary_metrics_are_not_coerced(field, value):
    payload = {"calendarDate": DAY.isoformat(), field: value}
    with make_client(lambda r: httpx.Response(200, json=payload) if r.url.path == module.SUMMARY_PATH else None)[0] as client:
        with pytest.raises(GarminError, match="invalid_response"):
            client.fetch_range(DAY, DAY)


@pytest.mark.parametrize("payload", [None, {}, [None], [{}], [{"calendarDate": "2026-10-31"}], [{"calendarDate": "2026-11-02"}], [{"calendarDate": DAY.isoformat(), "sleepTimeSeconds": True}], [{"calendarDate": DAY.isoformat(), "sleepTimeSeconds": "123"}], [{"calendarDate": DAY.isoformat(), "sleepTimeSeconds": 86401}], [{"calendarDate": DAY.isoformat()}] * 2])
def test_malformed_sleep_never_returns_partial_summary_data(payload):
    with make_client(lambda r: httpx.Response(200, json=payload) if r.url.path == module.SLEEP_PATH else None)[0] as client:
        with pytest.raises(GarminError) as caught:
            client.fetch_range(DAY, DAY)
    assert caught.value.kind == "invalid_response"
    assert caught.value.source == "sleep"


@pytest.mark.parametrize("path", [module.SUMMARY_PATH, module.SLEEP_PATH])
def test_invalid_json_drops_body_and_parser_exception(path):
    with make_client(lambda r: httpx.Response(200, text=KEY) if r.url.path == path else None)[0] as client:
        with pytest.raises(GarminError) as caught:
            client.fetch_range(DAY, DAY)
    assert caught.value.kind == "invalid_response"
    assert caught.value.__context__ is None
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("start,end", [(DAY, DAY - timedelta(days=1)), ("2026-11-01", DAY), (DAY, "2026-11-01"), (datetime(2026, 11, 1), DAY), (DAY, DAY + timedelta(days=366))])
def test_invalid_period_is_rejected_before_auth(start, end):
    client, requests = make_client()
    with client, pytest.raises(GarminError, match="invalid_range"):
        client.fetch_range(start, end)
    assert requests == []


@pytest.mark.parametrize("start,count", [(date(2024, 2, 28), 3), (date(2025, 12, 31), 2), (date(2026, 10, 26), 7), (date(2026, 1, 1), 31), (date(2026, 1, 1), 32), (date(2024, 1, 1), 366), (date.max, 1)])
def test_date_boundaries_sorting_and_documented_sleep_chunk_limit(start, count):
    end = start + timedelta(days=count - 1)
    client, requests = make_client()
    with client:
        found = client.fetch_range(start, end)
    expected = [(start + timedelta(days=i)).isoformat() for i in range(count)]
    assert [d["date"] for d in found] == expected
    assert [r.url.params["calendarDate"] for r in requests if r.url.path == module.SUMMARY_PATH] == expected
    covered = []
    for request in requests:
        if request.url.path == module.SLEEP_PATH:
            first = date.fromisoformat(request.url.params["startDate"])
            last = date.fromisoformat(request.url.params["endDate"])
            assert 0 <= (last - first).days <= 30
            covered.extend((first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1))
    assert covered == expected


def test_unsorted_sparse_sleep_joins_by_calendar_date_without_utc_conversion():
    start = date(2026, 10, 31)
    def handler(request):
        if request.url.path == module.SLEEP_PATH:
            return httpx.Response(200, json=[{"calendarDate": "2026-11-02", "sleepTimeSeconds": 18000, "sleepStartTimestampGMT": "2026-11-01T23:00:00Z"}, {"calendarDate": "2026-10-31", "sleepTimeSeconds": 25000}])
    with make_client(handler)[0] as client:
        found = client.fetch_range(start, date(2026, 11, 2))
    assert [d["sleep_seconds"] for d in found] == [25000, None, 18000]


def test_duplicate_sleep_dates_stop_instead_of_overwriting():
    def handler(request):
        if request.url.path == module.SLEEP_PATH:
            return httpx.Response(200, json=[
                {"calendarDate": DAY.isoformat(), "sleepTimeSeconds": 100},
                {"calendarDate": DAY.isoformat(), "sleepTimeSeconds": 200},
            ])

    with make_client(handler)[0] as client, pytest.raises(GarminError, match="invalid_response"):
        client.fetch_range(DAY, DAY + timedelta(days=2))


def test_later_source_failure_returns_no_partial_range():
    calls = 0

    def handler(request):
        nonlocal calls
        if request.url.path == module.SLEEP_PATH:
            calls += 1
            if calls == 1:
                return httpx.Response(200, json=[{"calendarDate": DAY.isoformat(), "sleepTimeSeconds": 100}])
            return httpx.Response(403, text=KEY)

    with make_client(handler)[0] as client, pytest.raises(GarminError) as caught:
        client.fetch_range(DAY, DAY + timedelta(days=31))
    assert caught.value.source == "sleep"
    assert caught.value.status_code == 403


def test_error_fields_are_allowlisted_and_client_has_no_general_write_surface():
    error = GarminError(KEY, status_code=999, source=KEY)
    assert error.kind == "unavailable"
    assert error.status_code is None
    assert error.source is None
    assert KEY not in repr(error)
    assert {name for name in dir(GarminClient) if not name.startswith("_")} == {"close", "fetch_range"}


def test_explicit_undated_empty_sleep_placeholder_is_missing_not_sync_failure():
    def callback(request):
        if request.url.path == module.SLEEP_PATH:
            return httpx.Response(200, json=[{'calendarDate': None, 'sleepTimeSeconds': None}])
    client, _ = make_client(callback)
    with client:
        assert client.fetch_range(DAY,DAY)[0]['sleep_seconds'] is None


def test_undated_sleep_with_a_duration_is_rejected_instead_of_guessed():
    def callback(request):
        if request.url.path == module.SLEEP_PATH:
            return httpx.Response(200, json=[{'calendarDate': None, 'sleepTimeSeconds': 12345}])
    client, _ = make_client(callback)
    with client, pytest.raises(GarminError):
        client.fetch_range(DAY,DAY)
