from datetime import datetime, timezone

import httpx
import pytest

from app.hevy.client import PAGE_SIZE, HevyAuthError, HevyClient, HevyError

KEY = "test-hevy-key-not-real"
START = "2024-05-01T12:00:00Z"


def client_for(handler, **kwargs) -> HevyClient:
    delays = kwargs.pop("delays", [])
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return HevyClient(
        api_key=KEY,
        http=http,
        sleep=delays.append,
        page_delay=kwargs.pop("page_delay", 0),
        max_attempts=kwargs.pop("max_attempts", 3),
    )


def test_workout_pagination_uses_documented_page_size():
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.headers["api-key"] == KEY
        assert request.url.path == "/v1/workouts"
        assert int(request.url.params["pageSize"]) == PAGE_SIZE
        page = int(request.url.params["page"])
        requested.append(page)
        workouts = [{"id": f"w{page}"}]
        return httpx.Response(200, json={"page": page, "page_count": 3, "workouts": workouts})

    found = list(client_for(handler).iter_workouts())
    assert requested == [1, 2, 3]
    assert [item["id"] for item in found] == ["w1", "w2", "w3"]


def test_empty_page_before_page_count_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        workouts = [{"id": "w1"}] if page == 1 else []
        return httpx.Response(200, json={"page": page, "page_count": 3, "workouts": workouts})

    with pytest.raises(HevyError, match="empty"):
        list(client_for(handler).iter_workouts())


def test_events_empty_page_without_an_events_list_is_finished():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/workouts/events"
        return httpx.Response(200, json={"page": 1, "page_count": 1, "workouts": []})

    assert list(client_for(handler).iter_events(START)) == []


def test_events_pass_since_and_stop_on_last_page():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/workouts/events"
        assert request.url.params["since"] == START
        assert int(request.url.params["pageSize"]) == 10
        return httpx.Response(
            200,
            json={
                "page": 1,
                "page_count": 1,
                "events": [{"type": "deleted", "id": "w1", "deleted_at": START}],
            },
        )

    events = list(client_for(handler).iter_events(START))
    assert events[0]["type"] == "deleted"


def test_page_delay_happens_between_pages_only():
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={"page": page, "page_count": 2, "workouts": [{"id": str(page)}]},
        )

    list(client_for(handler, delays=delays, page_delay=0.25).iter_workouts())
    assert delays == [0.25]


def test_retries_server_error_then_succeeds_without_leaking_key():
    calls = {"n": 0}
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"retry-after": "1.5"}, json={"error": KEY})
        return httpx.Response(200, json={"workout_count": 4})

    count = client_for(handler, delays=delays).get_workout_count()
    assert count == 4
    assert calls["n"] == 2
    assert delays == [1.5]


def test_auth_failure_is_not_retried_and_hides_the_key():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text=KEY)

    with pytest.raises(HevyAuthError) as raised:
        client_for(handler).get_user()
    assert calls["n"] == 1
    assert KEY not in str(raised.value)
    assert raised.value.__cause__ is None


def test_network_errors_retry_without_chaining_the_request():
    calls = {"n": 0}
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("network down")
        return httpx.Response(200, json={"data": {"weight_unit": "kg"}})

    user = client_for(handler, delays=delays).get_user()
    assert user["weight_unit"] == "kg"
    assert calls["n"] == 3
    assert delays == [0.5, 1.0]


def test_bad_request_is_not_retried_and_body_is_dropped():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text=KEY)

    with pytest.raises(HevyError) as raised:
        client_for(handler).get_workout_count()
    assert calls["n"] == 1
    assert "HTTP 400" in str(raised.value)
    assert KEY not in str(raised.value)


def test_missing_api_key_does_not_open_a_connection():
    with pytest.raises(HevyAuthError, match="HEVY_API_KEY"):
        HevyClient(api_key="  ")


def test_client_surface_has_no_write_methods():
    names = {name for name in dir(HevyClient) if not name.startswith("_")}
    assert {"post", "put", "delete", "patch"}.isdisjoint(names)


WORKOUT_ID = "b459cba5-cd6d-463c-abd6-54f8eafcadcb"


def test_get_workout_reads_the_documented_object():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/v1/workouts/{WORKOUT_ID}"
        assert request.headers["api-key"] == KEY
        return httpx.Response(200, json={"id": WORKOUT_ID, "title": "Push", "exercises": []})

    found = client_for(handler).get_workout(WORKOUT_ID)
    assert found["id"] == WORKOUT_ID
    assert found["title"] == "Push"


def test_get_workout_accepts_a_wrapped_object():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workout": {"id": WORKOUT_ID, "exercises": []}})

    found = client_for(handler).get_workout(WORKOUT_ID)
    assert found["id"] == WORKOUT_ID


def test_get_workout_rejects_a_bad_id_without_a_request():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"id": "nope"})

    with pytest.raises(HevyError, match="not valid"):
        client_for(handler).get_workout("../admin")
    assert calls["n"] == 0


def test_get_workout_404_is_not_retried_inside_the_client():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text=KEY)

    with pytest.raises(HevyError) as raised:
        client_for(handler).get_workout(WORKOUT_ID)
    assert calls["n"] == 1
    assert raised.value.status_code == 404
    assert KEY not in str(raised.value)
