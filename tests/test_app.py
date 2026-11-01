from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.present import CONFIRMED_LABEL, HEVY_RECORD_NOTICE, format_set, format_weight

PASSWORD = "test-password-123"
SECRET = "test-secret-value-32chars"
KEY = "should-not-appear-in-html"


def settings(tmp_path, **overrides):
    values = dict(
        hevy_api_key="",
        app_password=PASSWORD,
        app_secret=SECRET,
        database_path=str(tmp_path / "coach.db"),
        sync_enabled=False,
        webhook_worker_enabled=False,
        hevy_page_delay_seconds=0,
        secure_cookies=False,
        openai_api_key="",
        openai_model="",
    )
    values.update(overrides)
    return Settings(**values)


def signed_in(tmp_path, **overrides):
    app = create_app(settings(tmp_path, **overrides))
    client = TestClient(app)
    response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    return client, app


def test_setup_gate_when_password_is_missing(tmp_path):
    app = create_app(settings(tmp_path, app_password="", app_secret=""))
    client = TestClient(app)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    page = client.get("/setup")
    assert "HEVY_API_KEY" in page.text
    assert "HEVY_WEBHOOK_AUTHORIZATION" in page.text
    assert KEY not in page.text


def test_login_and_saved_workout_are_marked_as_hevy_records(tmp_path):
    client, app = signed_in(tmp_path, hevy_api_key=KEY)
    app.state.db.apply_workout(
        {
            "id": "w1",
            "title": "<script>alert(1)</script>",
            "description": "notes",
            "start_time": "2024-01-01T15:00:00Z",
            "end_time": "2024-01-01T16:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
            "created_at": "2024-01-01T15:00:00Z",
            "exercises": [
                {
                    "index": 0,
                    "title": "Bench Press (Barbell)",
                    "notes": "smooth",
                    "exercise_template_id": "05293BCA",
                    "superset_id": None,
                    "sets": [
                        {
                            "index": 0,
                            "type": "normal",
                            "weight_kg": 100,
                            "reps": 5,
                            "distance_meters": None,
                            "duration_seconds": None,
                            "rpe": 8,
                            "custom_metric": None,
                        }
                    ],
                }
            ],
        },
        "2026-10-31T18:31:00Z",
    )
    app.state.db.save_profile({"name": "Lifter", "weight_unit": "lbs", "distance_unit": "miles"}, "2026-10-31T18:31:00Z")
    home = client.get("/")
    assert home.status_code == 200
    assert HEVY_RECORD_NOTICE in home.text
    assert "Hevy callbacks stay disabled" in home.text
    assert "<script>alert(1)</script>" not in home.text
    assert "&lt;script&gt;" in home.text
    assert KEY not in home.text
    assert PASSWORD not in home.text
    detail = client.get("/workouts/w1")
    assert CONFIRMED_LABEL in detail.text
    assert "220.5 lb" in detail.text
    assert "100 kg" not in detail.text
    stored = app.state.db.get_workout("w1")
    assert stored["exercises"][0]["sets"][0]["weight_kg"] == 100


def test_wrong_password_is_not_echoed(tmp_path):
    app = create_app(settings(tmp_path))
    client = TestClient(app)
    response = client.post("/login", data={"password": "wrong-password-xyz"})
    assert response.status_code == 401
    assert "wrong-password-xyz" not in response.text
    assert "did not match" in response.text


def test_anonymous_visitors_do_not_see_workouts(tmp_path):
    app = create_app(settings(tmp_path))
    client = TestClient(app)
    response = client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/login"
    assert client.get("/health").json() == {"status": "ok"}
    assert KEY not in client.get("/health").text


def test_manual_sync_uses_injected_reader_and_shows_timestamps(tmp_path):
    client, app = signed_in(tmp_path, hevy_api_key=KEY)
    calls = []

    class Reader:
        def iter_workouts(self):
            calls.append("workouts")
            return iter(())

        def iter_events(self, since):
            calls.append(("events", since))
            return iter(())

        def get_workout_count(self):
            calls.append("count")
            return 0

        def get_user(self):
            calls.append("user")
            return {"weight_unit": "kg", "distance_unit": "kilometers", "name": "Lifter"}

        def post(self, *_args, **_kwargs):
            raise AssertionError("write")

        def close(self):
            calls.append("close")

    app.state.client_factory = Reader
    response = client.post("/sync", follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/")
    assert "Sync finished" in page.text
    assert "Last success" in page.text
    assert "Not yet" not in page.text
    assert KEY not in page.text
    assert "workouts" in calls
    assert "post" not in calls


def test_deleted_workout_is_hidden_from_the_list(tmp_path):
    client, app = signed_in(tmp_path)
    app.state.db.apply_workout(
        {
            "id": "gone",
            "title": "Old session",
            "updated_at": "2024-01-01T00:00:00Z",
            "start_time": "2024-01-01T00:00:00Z",
            "exercises": [],
        },
        "2026-10-31T00:00:00Z",
    )
    app.state.db.mark_deleted("gone", "2024-02-01T00:00:00Z", "2026-10-31T00:00:00Z")
    home = client.get("/")
    assert "Old session" not in home.text
    detail = client.get("/workouts/gone")
    assert "deleted" in detail.text.lower()


def test_weight_formatting_does_not_change_stored_kilograms():
    assert format_weight(100, "kg") == "100 kg"
    assert format_weight(100, "lbs") == "220.5 lb"
    assert "100 kg" in format_set(
        {"set_type": "normal", "weight_kg": 100, "reps": 5, "rpe": None, "custom_metric": None},
        "kg",
        "kilometers",
    )
