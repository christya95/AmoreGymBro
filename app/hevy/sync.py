"""Synchronize saved Hevy workouts into the configured local database.

The first successful run reads the full workout list. Later runs read the
documented workout event feed, which reports updates and deletes. The cursor
moves forward only after a run finishes without an error.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.db import Database
from app.hevy.client import HevyError
from app.redact import redact
from app.timeutil import shift, to_iso

OVERLAP_SECONDS = 120


def run_sync(
    db: Database,
    client,
    *,
    clock: Callable[[], datetime] | None = None,
    redact_secrets: list[str] | None = None,
    force_full: bool | None = None,
) -> dict:
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = clock()
    started_iso = to_iso(started)
    state = db.get_sync_state()
    full = force_full if force_full is not None else not state.get("events_since")
    mode = "full" if full else "incremental"
    run_id = db.start_run(started_iso, mode)
    upserted = 0
    deleted = 0
    ignored = 0
    warning_parts: list[str] = []
    remote_count: int | None = None
    error: str | None = None
    cursor = to_iso(shift(started, -OVERLAP_SECONDS))
    event_since = cursor if full else state.get("events_since")

    try:
        if full:
            for workout in client.iter_workouts():
                upserted, deleted, ignored = _tally(
                    db.apply_workout(workout, started_iso),
                    upserted,
                    deleted,
                    ignored,
                )
        for event in client.iter_events(event_since):
            upserted, deleted, ignored = _apply_event(
                db, event, started_iso, upserted, deleted, ignored
            )
        try:
            remote_count = client.get_workout_count()
        except HevyError:
            warning_parts.append("Hevy did not return a workout count.")
        try:
            db.save_profile(client.get_user(), started_iso)
        except HevyError:
            warning_parts.append("Hevy profile could not be refreshed.")
        fetch_measurements = getattr(client, "iter_body_measurements", None)
        if fetch_measurements is not None:
            try:
                for measurement in fetch_measurements():
                    db.upsert_body_measurement(measurement, started_iso)
            except HevyError:
                warning_parts.append("Hevy body measurements could not be refreshed.")
    except Exception as exc:
        error = redact(str(exc) or exc.__class__.__name__, redact_secrets)

    finished_iso = to_iso(clock())
    local_count = db.count_active()
    warning = " ".join(warning_parts) or None
    if error is None and remote_count is not None and remote_count != local_count:
        remote_noun = "workout" if remote_count == 1 else "workouts"
        local_noun = "workout" if local_count == 1 else "workouts"
        mismatch = (
            f"Hevy reports {remote_count} {remote_noun} and this copy has "
            f"{local_count} active {local_noun}."
        )
        warning = f"{warning} {mismatch}".strip() if warning else mismatch
    db.finish_run(
        run_id,
        finished_at=finished_iso,
        status="error" if error else "success",
        upserted=upserted,
        deleted=deleted,
        ignored=ignored,
        error=error,
        warning=warning,
        events_since=cursor,
        remote_count=remote_count,
        local_count=local_count,
        advance_cursor=error is None,
    )
    return db.get_sync_state() | {
        "mode": mode,
        "status": "error" if error else "success",
        "upserted": upserted,
        "deleted": deleted,
        "ignored": ignored,
        "error": error,
        "warning": warning,
    }


def _apply_event(db: Database, event: dict, synced_at: str, upserted: int, deleted: int, ignored: int):
    kind = event.get("type") if isinstance(event, dict) else None
    if kind == "updated":
        workout = event.get("workout")
        if not isinstance(workout, dict):
            return upserted, deleted, ignored + 1
        outcome = db.apply_workout(workout, synced_at)
    elif kind == "deleted":
        workout_id = event.get("id")
        if not workout_id:
            return upserted, deleted, ignored + 1
        outcome = db.mark_deleted(str(workout_id), event.get("deleted_at"), synced_at)
    else:
        return upserted, deleted, ignored + 1
    return _tally(outcome, upserted, deleted, ignored)


def _tally(outcome: str, upserted: int, deleted: int, ignored: int):
    if outcome in {"inserted", "updated"}:
        return upserted + 1, deleted, ignored
    if outcome == "deleted":
        return upserted, deleted + 1, ignored
    return upserted, deleted, ignored + 1
