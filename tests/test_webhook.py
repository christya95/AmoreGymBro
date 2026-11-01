from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.db import Database
from app.hevy.client import HevyAuthError, HevyError
from app.hevy.webhook import WebhookWorker, process_webhook_jobs
from app.timeutil import shift, to_iso
from tests.test_app import settings

API_KEY = "hevy-api-key-value-32"
WEBHOOK_SECRET = "webhook-secret-value-32"
WORKOUT_ID = "b459cba5-cd6d-463c-abd6-54f8eafcadcb"
OTHER_ID = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)


def saved_workout(workout_id=WORKOUT_ID):
    return {
        "id": workout_id,
        "title": "Push",
        "updated_at": "2024-01-02T00:00:00Z",
        "start_time": "2024-01-01T15:00:00Z",
        "end_time": "2024-01-01T16:00:00Z",
        "created_at": "2024-01-01T15:00:00Z",
        "exercises": [],
    }


class Reader:
    def __init__(self, workout=None, error=None, mismatch=False):
        self.workout = saved_workout() if workout is None else workout
        self.error = error
        self.mismatch = mismatch
        self.calls = []
        self.writes = []

    def get_workout(self, workout_id):
        self.calls.append(workout_id)
        if self.error:
            raise self.error
        if self.mismatch:
            return saved_workout(OTHER_ID)
        return self.workout

    def post(self, *_args, **_kwargs):
        self.writes.append("post")
        raise AssertionError("write")

    def close(self):
        self.calls.append("close")


def webhook_app(tmp_path, **overrides):
    values = dict(
        hevy_api_key=API_KEY,
        hevy_webhook_authorization=WEBHOOK_SECRET,
        webhook_worker_enabled=False,
    )
    values.update(overrides)
    app = create_client_app(tmp_path, **values)
    return app


def create_client_app(tmp_path, **overrides):
    from app.main import create_app

    return create_app(settings(tmp_path, **overrides))


def post_notice(client, workout_id=WORKOUT_ID, secret=WEBHOOK_SECRET, body=None):
    payload = body if body is not None else {"workoutId": workout_id}
    headers = {}
    if secret is not None:
        headers["Authorization"] = secret
    if isinstance(payload, (bytes, str)):
        return client.post(
            "/webhooks/hevy",
            content=payload,
            headers={**headers, "content-type": "application/json"},
        )
    return client.post("/webhooks/hevy", json=payload, headers=headers)


def test_valid_callback_is_stored_and_does_not_fetch_yet(tmp_path):
    app = webhook_app(tmp_path)
    reader = Reader()
    app.state.client_factory = lambda: reader
    client = TestClient(app)
    response = post_notice(client)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "duplicate": False}
    assert WEBHOOK_SECRET not in response.text
    assert API_KEY not in response.text
    job = app.state.db.get_webhook_job(WORKOUT_ID)
    assert job["status"] == "pending"
    assert reader.calls == []
    assert app.state.db.get_workout(WORKOUT_ID) is None


def test_callback_does_not_require_the_phone_login(tmp_path):
    app = webhook_app(tmp_path)
    client = TestClient(app)
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303
    assert home.headers["location"] == "/login"
    assert post_notice(client).status_code == 200


def test_invalid_authorization_stores_nothing(tmp_path):
    app = webhook_app(tmp_path)
    client = TestClient(app)
    response = post_notice(client, secret="wrong-webhook-secret-value")
    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"
    assert "wrong-webhook-secret-value" not in response.text
    assert WEBHOOK_SECRET not in response.text
    assert app.state.db.get_webhook_job(WORKOUT_ID) is None


def test_missing_authorization_stores_nothing(tmp_path):
    app = webhook_app(tmp_path)
    client = TestClient(app)
    response = post_notice(client, secret=None)
    assert response.status_code == 401
    assert app.state.db.get_webhook_job(WORKOUT_ID) is None


def test_unconfigured_callback_rejects_every_header(tmp_path):
    app = webhook_app(tmp_path, hevy_webhook_authorization="")
    client = TestClient(app)
    response = post_notice(client, secret=WEBHOOK_SECRET)
    assert response.status_code == 401
    assert app.state.db.get_webhook_job(WORKOUT_ID) is None


def test_api_key_is_not_accepted_as_the_callback_secret(tmp_path):
    app = webhook_app(tmp_path, hevy_api_key=API_KEY, hevy_webhook_authorization=API_KEY)
    assert app.state.settings.webhook_configured is False
    client = TestClient(app)
    response = post_notice(client, secret=API_KEY)
    assert response.status_code == 401
    assert API_KEY not in response.text
    assert app.state.db.get_webhook_job(WORKOUT_ID) is None


def test_malformed_callbacks_are_rejected(tmp_path):
    app = webhook_app(tmp_path)
    client = TestClient(app)
    bodies = [
        b"{",
        b"[]",
        {"workoutId": 12},
        {"missing": WORKOUT_ID},
        {"workoutId": "not-a-uuid"},
        b"{" + b"x" * 3000,
        f'{{"workoutId": "{WEBHOOK_SECRET}"}}'.encode(),
    ]
    for body in bodies:
        response = post_notice(client, body=body)
        assert response.status_code == 400, body
        assert response.json()["detail"] == "Bad request"
        assert WEBHOOK_SECRET not in response.text
        assert API_KEY not in response.text
    assert app.state.db.webhook_counts()["pending"] == 0


def test_duplicate_callback_keeps_one_job(tmp_path):
    app = webhook_app(tmp_path)
    client = TestClient(app)
    assert post_notice(client).json()["duplicate"] is False
    second = post_notice(client, body={"workoutId": WORKOUT_ID, "extra": "ignored"})
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert app.state.db.webhook_counts()["pending"] == 1


def test_worker_fetches_once_and_a_restart_finishes_a_running_job(tmp_path):
    path = tmp_path / "coach.db"
    db = Database(str(path))
    db.migrate()
    assert db.enqueue_webhook_job(WORKOUT_ID, to_iso(NOW)) == "queued"
    assert db.claim_due_webhook_jobs(to_iso(NOW)) == [WORKOUT_ID]
    assert db.get_webhook_job(WORKOUT_ID)["status"] == "running"

    restarted = Database(str(path))
    restarted.migrate()
    assert restarted.recover_running_webhook_jobs(to_iso(NOW)) == 1
    assert restarted.get_webhook_job(WORKOUT_ID)["status"] == "pending"

    reader = Reader()
    outcomes = process_webhook_jobs(
        restarted,
        reader,
        now=NOW,
        redact_secrets=[API_KEY, WEBHOOK_SECRET],
    )
    assert outcomes == ["done"]
    assert reader.calls == [WORKOUT_ID]
    assert reader.writes == []
    stored = restarted.get_workout(WORKOUT_ID)
    assert stored is not None
    assert stored["title"] == "Push"
    assert process_webhook_jobs(restarted, reader, now=shift(NOW, 60)) == []
    assert reader.calls == [WORKOUT_ID]
    assert restarted.webhook_counts()["done"] == 1


def test_transient_failure_retries_and_auth_failure_stops(tmp_path):
    db = Database(str(tmp_path / "coach.db"))
    db.migrate()
    db.enqueue_webhook_job(WORKOUT_ID, to_iso(NOW))
    flaky = Reader(error=HevyError(f"missing {WEBHOOK_SECRET}", status_code=404))
    assert process_webhook_jobs(db, flaky, now=NOW, redact_secrets=[WEBHOOK_SECRET]) == ["retry"]
    job = db.get_webhook_job(WORKOUT_ID)
    assert job["status"] == "retry"
    assert job["attempts"] == 1
    assert job["next_attempt_at"] == to_iso(shift(NOW, 15))
    assert WEBHOOK_SECRET not in (job["last_error"] or "")
    assert process_webhook_jobs(db, flaky, now=shift(NOW, 14), redact_secrets=[WEBHOOK_SECRET]) == []
    assert len(flaky.calls) == 1

    flaky.error = None
    assert process_webhook_jobs(db, flaky, now=shift(NOW, 15), redact_secrets=[WEBHOOK_SECRET]) == ["done"]
    assert db.get_workout(WORKOUT_ID) is not None

    db.enqueue_webhook_job(OTHER_ID, to_iso(NOW))
    rejected = Reader(error=HevyAuthError(f"rejected {API_KEY}"))
    assert process_webhook_jobs(db, rejected, now=NOW, redact_secrets=[API_KEY]) == ["failed"]
    assert process_webhook_jobs(db, rejected, now=shift(NOW, 1000), redact_secrets=[API_KEY]) == []
    assert rejected.calls == [OTHER_ID]
    failed = db.get_webhook_job(OTHER_ID)
    assert failed["status"] == "failed"
    assert API_KEY not in (failed["last_error"] or "")


def test_failed_notice_can_be_queued_again_by_a_later_callback(tmp_path):
    app = webhook_app(tmp_path)
    client = TestClient(app)
    post_notice(client)
    reader = Reader(error=HevyError("Hevy returned HTTP 400.", status_code=400))
    assert process_webhook_jobs(app.state.db, reader, now=NOW) == ["failed"]
    assert app.state.db.get_workout(WORKOUT_ID) is None
    again = post_notice(client)
    assert again.status_code == 200
    assert again.json()["duplicate"] is False
    job = app.state.db.get_webhook_job(WORKOUT_ID)
    assert job["status"] == "pending"
    assert job["attempts"] == 0


def test_mismatched_workout_is_not_stored(tmp_path):
    db = Database(str(tmp_path / "coach.db"))
    db.migrate()
    db.enqueue_webhook_job(WORKOUT_ID, to_iso(NOW))
    reader = Reader(mismatch=True)
    assert process_webhook_jobs(db, reader, now=NOW) == ["failed"]
    assert db.get_workout(WORKOUT_ID) is None
    assert db.get_workout(OTHER_ID) is None


def test_phone_page_shows_a_failed_notice_without_the_secret(tmp_path):
    app = webhook_app(tmp_path)
    app.state.db.enqueue_webhook_job(WORKOUT_ID, to_iso(NOW))
    app.state.db.claim_due_webhook_jobs(to_iso(NOW))
    app.state.db.complete_webhook_job(
        WORKOUT_ID,
        status="failed",
        now=to_iso(NOW),
        error=f"boom {WEBHOOK_SECRET}",
        next_attempt_at=None,
    )
    client = TestClient(app)
    client.post("/login", data={"password": "test-password-123"})
    page = client.get("/")
    assert "1 saved-workout notice could not be fetched" in page.text
    assert WEBHOOK_SECRET not in page.text
    assert "boom" not in page.text


def test_webhook_worker_invokes_its_trigger():
    called = []
    worker = WebhookWorker(lambda: called.append("tick"), interval_seconds=0.01)
    worker.start()
    try:
        for _ in range(50):
            if called:
                break
            import time

            time.sleep(0.01)
    finally:
        worker.stop()
    assert called
