import httpx
import pytest
from fastapi.testclient import TestClient

from app.hevy.client import HevyClient, HevyError
from app.hevy.library import routine_library
from app.main import create_app
from tests.test_app import settings, signed_in
from tests.test_ui import Page


def api(handler):
    return HevyClient("private-test-key", http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None, page_delay=0, max_attempts=1)


def listing(request):
    assert request.method == "GET"
    assert request.url.params["pageSize"] == "10"
    page = int(request.url.params["page"])
    if request.url.path == "/v1/routine_folders":
        return httpx.Response(200, json={"page_count": 2, "routine_folders": [
            {"id": page, "title": f"Folder {page}"}]})
    assert request.url.path == "/v1/routines"
    return httpx.Response(200, json={"page_count": 2, "routines": [
        {"id": f"r{page}-{i}", "title": f"Routine {page}-{i}", "folder_id": page,
         "exercises": [{"title": "Press", "rest_seconds": 90, "sets": [
             {"type": "normal", "weight_kg": 10, "reps": None, "rep_range": {"start": 8, "end": 12}}]}]}
        for i in range(10 if page == 1 else 2)]})


def test_library_fetches_every_page_and_folder_without_counting_as_workouts(tmp_path):
    client, app = signed_in(tmp_path, hevy_api_key="configured-test-key")
    app.state.client_factory = lambda: api(listing)
    app.state.db.save_profile({"weight_unit": "lbs"}, "2026-11-01T00:00:00Z")
    response = client.get("/hevy/routines")
    assert response.status_code == 200
    Page(response.text)
    assert "12 routines in Hevy" in response.text
    assert "Routine 2-1" in response.text and "Folder 2" in response.text
    assert "22.0 lb · 8–12 reps" in response.text
    assert "Rest: 90 seconds" in response.text
    assert "aren’t completed workouts" in response.text
    assert app.state.db.count_active() == 0
    assert app.state.db.latest_training_plan() is None
    assert 'href="/" class="active"' in response.text
    home = client.get("/").text
    assert "Completed workouts" in home and 'href="/hevy/routines"' in home


def test_library_auth_gate_does_not_contact_hevy(tmp_path):
    app = create_app(settings(tmp_path))
    app.state.client_factory = lambda: pytest.fail("unauthenticated API call")
    response = TestClient(app).get("/hevy/routines", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_later_page_failure_hides_incomplete_list_and_secrets(tmp_path):
    def fail(request):
        if request.url.path == "/v1/routines" and request.url.params["page"] == "2":
            return httpx.Response(503, text="private-test-key")
        return listing(request)
    client, app = signed_in(tmp_path, hevy_api_key="configured-test-key")
    app.state.client_factory = lambda: api(fail)
    response = client.get("/hevy/routines")
    assert response.status_code == 502
    assert "Couldn’t load the full routine library" in response.text
    assert "Routine 1-0" not in response.text
    assert "No routines saved" not in response.text and "private-test-key" not in response.text


def test_library_empty_unfiled_duplicates_and_missing_targets():
    def handler(request):
        if request.url.path == "/v1/routine_folders":
            return httpx.Response(200, json={"routine_folders": []})
        row = {"id": "one", "title": "Original", "folder_id": None, "exercises": [
            {"title": "Empty targets", "sets": [{"type": "normal", "weight_kg": None, "reps": None}]}]}
        return httpx.Response(200, json={"routines": [row, row]})
    rows = routine_library(api(handler))
    assert len(rows) == 1 and rows[0]["folder"] == "Unfiled"
    assert rows[0]["exercises"][0]["sets"] == ["Targets not set"]


def test_unexpected_empty_page_is_not_a_successful_partial_library():
    def handler(request):
        if request.url.path == "/v1/routine_folders":
            return httpx.Response(200, json={"routine_folders": []})
        return httpx.Response(200, json={"page_count": 2, "routines": []})
    with pytest.raises(HevyError):
        routine_library(api(handler))


def test_empty_library_is_distinct_from_not_connected(tmp_path):
    client, app = signed_in(tmp_path, hevy_api_key="configured-test-key")
    app.state.client_factory = lambda: api(lambda request: httpx.Response(200, json={
        "routine_folders" if request.url.path.endswith("routine_folders") else "routines": []}))
    assert "No routines saved in Hevy" in client.get("/hevy/routines").text
    client, app = signed_in(tmp_path / "offline", hevy_api_key="")
    app.state.client_factory = lambda: pytest.fail("missing credentials")
    response = client.get("/hevy/routines")
    assert "Hevy is not connected" in response.text and "No routines saved" not in response.text
