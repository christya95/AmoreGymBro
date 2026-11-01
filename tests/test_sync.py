from datetime import datetime, timezone

import pytest

from app.db import Database
from app.hevy.client import HevyError
from app.hevy.sync import run_sync
from app.timeutil import shift, to_iso

SECRET = "sync-secret-should-not-leak"
START = datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc)
END = datetime(2026, 10, 31, 18, 31, tzinfo=timezone.utc)


def workout(workout_id="w1", updated="2024-01-02T00:00:00Z", title="Push", weight=100):
    return {
        "id": workout_id,
        "title": title,
        "routine_id": "routine-1",
        "description": "Solid session",
        "start_time": "2024-01-01T15:00:00Z",
        "end_time": "2024-01-01T16:00:00Z",
        "updated_at": updated,
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
                        "weight_kg": weight,
                        "reps": 5,
                        "distance_meters": None,
                        "duration_seconds": None,
                        "rpe": 8,
                        "custom_metric": None,
                    }
                ],
            }
        ],
    }


class FakeHevy:
    def __init__(self, workouts=None, events=None, count=0, user=None, event_error=None):
        self.workouts = list(workouts or [])
        self.events = list(events or [])
        self.count = count
        self.user = user or {
            "id": "user-1",
            "username": "lifter",
            "name": "Lifter",
            "weight_unit": "kg",
            "distance_unit": "kilometers",
        }
        self.event_error = event_error
        self.calls = []

    def iter_workouts(self):
        self.calls.append("workouts")
        yield from self.workouts

    def iter_events(self, since):
        self.calls.append(("events", since))
        if self.event_error:
            raise self.event_error
        yield from self.events

    def get_workout_count(self):
        self.calls.append("count")
        return self.count

    def get_user(self):
        self.calls.append("user")
        return self.user

    def post(self, *_args, **_kwargs):
        raise AssertionError("sync attempted a write")

    def put(self, *_args, **_kwargs):
        raise AssertionError("sync attempted a write")


def clock_for(*moments):
    pending = iter(moments)

    def clock():
        return next(pending)

    return clock


def synced(db, fake, *moments, **kwargs):
    return run_sync(
        db,
        fake,
        clock=clock_for(*moments),
        redact_secrets=[SECRET],
        **kwargs,
    )


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "coach.db"))
    database.migrate()
    return database


def test_full_sync_records_timestamps_weight_and_read_calls_only(db):
    fake = FakeHevy(workouts=[workout(), workout()], count=1)
    result = synced(db, fake, START, END)
    assert result["status"] == "success"
    assert result["upserted"] == 1
    assert result["ignored"] == 1
    assert fake.calls == ["workouts", ("events", "2026-10-31T18:28:00Z"), "count", "user"]
    state = db.get_sync_state()
    assert state["last_attempt_at"] == to_iso(START)
    assert state["last_success_at"] == to_iso(END)
    assert state["events_since"] == to_iso(shift(START, -120))
    assert state["last_error"] is None
    stored = db.get_workout("w1")
    assert stored["exercises"][0]["sets"][0]["weight_kg"] == 100
    assert db.get_profile()["weight_unit"] == "kg"


def test_incremental_sync_applies_newer_delete_and_skips_full_list(db):
    first = FakeHevy(workouts=[workout()], count=1)
    synced(db, first, START, END)
    later_start = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    later_end = datetime(2026, 11, 1, 12, 1, tzinfo=timezone.utc)
    second = FakeHevy(
        events=[
            {"type": "deleted", "id": "w1", "deleted_at": "2024-06-01T00:00:00Z"},
            {
                "type": "updated",
                "workout": workout(updated="2024-01-01T00:00:00Z", title="Old title"),
            },
        ],
        count=0,
    )
    result = synced(db, second, later_start, later_end)
    assert "workouts" not in second.calls
    assert result["deleted"] == 1
    assert db.count_active() == 0
    assert db.get_workout("w1")["deleted_at"] == "2024-06-01T00:00:00Z"
    assert db.get_workout("w1")["title"] == "Push"


def test_newer_update_restores_a_deleted_workout(db):
    synced(
        db,
        FakeHevy(
            workouts=[workout()],
            events=[{"type": "deleted", "id": "w1", "deleted_at": "2024-06-01T00:00:00Z"}],
            count=0,
        ),
        START,
        END,
    )
    assert db.count_active() == 0
    restored = workout(updated="2024-07-01T00:00:00Z", title="Push again", weight=102.5)
    result = synced(
        db,
        FakeHevy(events=[{"type": "updated", "workout": restored}], count=1),
        datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 2, 8, 1, tzinfo=timezone.utc),
    )
    assert result["upserted"] == 1
    assert db.count_active() == 1
    stored = db.get_workout("w1")
    assert stored["deleted_at"] is None
    assert stored["title"] == "Push again"
    assert stored["exercises"][0]["sets"][0]["weight_kg"] == 102.5


def test_failed_sync_does_not_advance_cursor_and_redacts_secrets(db):
    fake = FakeHevy(workouts=[workout()], event_error=HevyError(f"down {SECRET}"))
    result = synced(db, fake, START, END)
    assert result["status"] == "error"
    state = db.get_sync_state()
    assert state["events_since"] is None
    assert state["last_success_at"] is None
    assert state["last_attempt_at"] == to_iso(START)
    assert SECRET not in state["last_error"]
    assert "[redacted]" in state["last_error"]
    runs = db.recent_runs()
    assert runs[0]["status"] == "error"


def test_count_mismatch_stays_visible_after_success(db):
    result = synced(db, FakeHevy(workouts=[workout()], count=9), START, END)
    assert result["status"] == "success"
    assert "9" in result["warning"]
    assert "1" in result["warning"]
    assert db.get_sync_state()["events_since"] == to_iso(shift(START, -120))


def test_duplicate_event_is_idempotent(db):
    payload = workout()
    synced(db, FakeHevy(workouts=[payload], count=1), START, END)
    again = synced(
        db,
        FakeHevy(events=[{"type": "updated", "workout": payload}], count=1),
        datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 3, 9, 1, tzinfo=timezone.utc),
    )
    assert again["upserted"] == 0
    assert again["ignored"] == 1
    assert db.count_active() == 1
