"""Local receiver for Hevy's saved-workout callback.

Hevy's public OpenAPI document does not describe this callback. The developer
settings screen sends an HTTP POST whose JSON body is {"workoutId": "<uuid>"}
and can include an authorization header chosen by the owner. This receiver
checks that header, stores one durable job, and answers immediately. A later
worker reads GET /v1/workouts/{workoutId}. Edits and deletions stay on the
documented workout event feed. The callback is not a live set stream.
"""

from __future__ import annotations

import json
import logging
import random
import secrets
import threading
import uuid
from collections.abc import Callable
from datetime import datetime

from app.db import Database
from app.hevy.client import HevyAuthError, HevyError
from app.redact import redact
from app.timeutil import shift, to_iso

logger = logging.getLogger("fitness_coach")

MAX_BODY_BYTES = 2048
MAX_FIELDS = 8
MAX_ATTEMPTS = 5
RETRY_DELAYS = (15, 45, 120, 300, 600)
RETRYABLE_STATUS = {404, 408, 429, 500, 502, 503, 504}


class WebhookPayloadError(Exception):
    pass


def authorization_matches(provided: str | None, expected: str) -> bool:
    if not expected or provided is None:
        return False
    provided_bytes = provided.strip().encode()
    expected_bytes = expected.strip().encode()
    if len(provided_bytes) != len(expected_bytes):
        return False
    return secrets.compare_digest(provided_bytes, expected_bytes)


def parse_workout_notice(body: bytes) -> str:
    if len(body) > MAX_BODY_BYTES:
        raise WebhookPayloadError("payload is too large")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise WebhookPayloadError("payload is not json") from None
    if not isinstance(payload, dict) or len(payload) > MAX_FIELDS:
        raise WebhookPayloadError("payload is not a small object")
    workout_id = payload.get("workoutId")
    if not isinstance(workout_id, str):
        raise WebhookPayloadError("workoutId is missing")
    try:
        return str(uuid.UUID(workout_id))
    except ValueError:
        raise WebhookPayloadError("workoutId is not a uuid") from None


def webhook_status_text(configured: bool, counts: dict) -> str:
    waiting = counts.get("pending", 0) + counts.get("running", 0) + counts.get("retry", 0)
    if not configured:
        return (
            "Hevy callbacks stay disabled until a separate authorization secret is set in the "
            "local .env file. No callback URL is registered. Sync on this page still copies "
            "saved workouts, edits, and deletions."
        )
    noun = "notice is" if waiting == 1 else "notices are"
    return (
        f"The local callback can store a saved-workout notice. {waiting} {noun} waiting to be "
        "fetched. Hevy is not subscribed, and this service is not on the public internet. "
        "Edits and deletions still come from the workout event feed."
    )


def process_webhook_jobs(
    db: Database,
    client,
    *,
    now: datetime,
    redact_secrets: list[str] | None = None,
    jitter_seconds: int = 0,
) -> list[str]:
    """Fetch due workouts. Returns the resulting status for each claimed job."""
    now_iso = to_iso(now)
    outcomes: list[str] = []
    for workout_id in db.claim_due_webhook_jobs(now_iso):
        try:
            workout = client.get_workout(workout_id)
        except HevyAuthError as exc:
            _finish(db, workout_id, now_iso, "failed", redact(str(exc), redact_secrets), None)
            logger.warning("webhook fetch was rejected by Hevy")
            outcomes.append("failed")
            continue
        except HevyError as exc:
            outcomes.append(
                _retry_or_fail(db, workout_id, now, exc, redact_secrets, jitter_seconds)
            )
            continue
        except Exception as exc:
            wrapped = HevyError(exc.__class__.__name__)
            outcomes.append(
                _retry_or_fail(db, workout_id, now, wrapped, redact_secrets, jitter_seconds)
            )
            continue
        if str(workout.get("id")) != workout_id:
            _finish(
                db,
                workout_id,
                now_iso,
                "failed",
                "Hevy returned a different workout.",
                None,
            )
            outcomes.append("failed")
            continue
        db.apply_workout(workout, now_iso)
        _finish(db, workout_id, now_iso, "done", None, None)
        outcomes.append("done")
    return outcomes


def _retry_or_fail(db, workout_id, now, exc: HevyError, redact_secrets, jitter_seconds: int) -> str:
    job = db.get_webhook_job(workout_id) or {}
    attempts = int(job.get("attempts") or 1)
    retryable = exc.status_code in RETRYABLE_STATUS or exc.status_code is None
    message = redact(str(exc), redact_secrets)
    now_iso = to_iso(now)
    if retryable and attempts < MAX_ATTEMPTS:
        delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
        if jitter_seconds > 0:
            delay += random.randint(0, jitter_seconds)
        _finish(db, workout_id, now_iso, "retry", message, to_iso(shift(now, delay)))
        logger.warning("webhook fetch will retry")
        return "retry"
    _finish(db, workout_id, now_iso, "failed", message, None)
    logger.warning("webhook fetch failed")
    return "failed"


def _finish(db, workout_id, now_iso, status, error, next_attempt_at) -> None:
    db.complete_webhook_job(
        workout_id,
        status=status,
        now=now_iso,
        error=error,
        next_attempt_at=next_attempt_at,
    )


class WebhookWorker:
    def __init__(self, trigger: Callable[[], None], *, interval_seconds: float = 2.0):
        self.trigger = trigger
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hevy-webhook", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.trigger()
            except Exception:
                logger.error("webhook worker failed")
            if self._stop.wait(self.interval_seconds):
                return
