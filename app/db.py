"""SQLite storage for confirmed Hevy workouts, local goals, and check-ins."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.timeutil import parse_iso


SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  events_since TEXT,
  last_attempt_at TEXT,
  last_success_at TEXT,
  last_error TEXT,
  last_warning TEXT,
  last_mode TEXT,
  remote_workout_count INTEGER,
  local_workout_count INTEGER
);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  workouts_upserted INTEGER NOT NULL DEFAULT 0,
  workouts_deleted INTEGER NOT NULL DEFAULT 0,
  workouts_ignored INTEGER NOT NULL DEFAULT 0,
  error TEXT
);

CREATE TABLE IF NOT EXISTS workouts (
  id TEXT PRIMARY KEY,
  title TEXT,
  routine_id TEXT,
  description TEXT,
  start_time TEXT,
  end_time TEXT,
  hevy_created_at TEXT,
  hevy_updated_at TEXT,
  source_time TEXT,
  deleted_at TEXT,
  raw_json TEXT NOT NULL,
  local_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercises (
  workout_id TEXT NOT NULL,
  exercise_index INTEGER NOT NULL,
  title TEXT,
  notes TEXT,
  exercise_template_id TEXT,
  superset_id INTEGER,
  PRIMARY KEY (workout_id, exercise_index),
  FOREIGN KEY (workout_id) REFERENCES workouts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sets (
  workout_id TEXT NOT NULL,
  exercise_index INTEGER NOT NULL,
  set_index INTEGER NOT NULL,
  set_type TEXT,
  weight_kg REAL,
  reps REAL,
  distance_meters REAL,
  duration_seconds REAL,
  rpe REAL,
  custom_metric REAL,
  PRIMARY KEY (workout_id, exercise_index, set_index),
  FOREIGN KEY (workout_id, exercise_index)
    REFERENCES exercises(workout_id, exercise_index) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS profile (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  hevy_user_id TEXT,
  username TEXT,
  display_name TEXT,
  weight_unit TEXT,
  distance_unit TEXT,
  synced_at TEXT
);

CREATE TABLE IF NOT EXISTS hevy_webhook_jobs (
  workout_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  received_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  next_attempt_at TEXT,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY,
  statement TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_ins (
  id INTEGER PRIMARY KEY,
  feeling TEXT NOT NULL,
  notes TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS body_measurements (
  date TEXT PRIMARY KEY,
  weight_kg REAL,
  lean_mass_kg REAL,
  fat_percent REAL,
  neck_cm REAL,
  shoulder_cm REAL,
  chest_cm REAL,
  left_bicep_cm REAL,
  right_bicep_cm REAL,
  left_forearm_cm REAL,
  right_forearm_cm REAL,
  abdomen REAL,
  waist REAL,
  hips REAL,
  left_thigh REAL,
  right_thigh REAL,
  left_calf REAL,
  right_calf REAL,
  raw_json TEXT NOT NULL,
  local_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS body_entry (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  height_cm REAL,
  age_years INTEGER,
  sex TEXT,
  activity TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coach_replies (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,
  question TEXT NOT NULL,
  body_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS garmin_daily (
  date TEXT PRIMARY KEY,
  body_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS garmin_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  body_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workouts_start ON workouts(start_time);
CREATE INDEX IF NOT EXISTS idx_webhook_jobs_due ON hevy_webhook_jobs(status, next_attempt_at);
"""


def _as_int(value, fallback: int) -> int:
    if value is None:
        return fallback
    return int(value)


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def migrate(self) -> None:
        with self.transaction() as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO sync_state (id) VALUES (1)")
            present = {row[1] for row in conn.execute("PRAGMA table_info(body_entry)")}
            for name, declaration in (
                ("days_per_week", "INTEGER"),
                ("session_minutes", "INTEGER"),
                ("train_where", "TEXT"),
            ):
                if name not in present:
                    conn.execute(f"ALTER TABLE body_entry ADD COLUMN {name} {declaration}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def get_sync_state(self) -> dict:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()
        return dict(row) if row else {}

    def count_active(self, start: str | None = None, end: str | None = None) -> int:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM workouts WHERE deleted_at IS NULL "
                "AND (? IS NULL OR julianday(start_time) >= julianday(?)) "
                "AND (? IS NULL OR julianday(start_time) < julianday(?))",
                (start, start, end, end),
            ).fetchone()
        return int(row["n"])

    def list_workouts(self, limit: int, offset: int) -> list[dict]:
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT w.*, (
                  SELECT COUNT(*) FROM sets s WHERE s.workout_id = w.id
                ) AS set_count
                FROM workouts w
                WHERE w.deleted_at IS NULL
                ORDER BY w.start_time DESC, w.id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_workout(self, workout_id: str) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,)).fetchone()
            if row is None:
                return None
            exercises = conn.execute(
                """
                SELECT * FROM exercises
                WHERE workout_id = ?
                ORDER BY exercise_index
                """,
                (workout_id,),
            ).fetchall()
            sets = conn.execute(
                """
                SELECT * FROM sets
                WHERE workout_id = ?
                ORDER BY exercise_index, set_index
                """,
                (workout_id,),
            ).fetchall()
        grouped: dict[int, list[dict]] = {}
        for item in sets:
            grouped.setdefault(int(item["exercise_index"]), []).append(dict(item))
        payload = dict(row)
        payload["exercises"] = []
        for exercise in exercises:
            entry = dict(exercise)
            entry["sets"] = grouped.get(int(exercise["exercise_index"]), [])
            payload["exercises"].append(entry)
        return payload

    def get_profile(self) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
        return dict(row) if row else None

    def recent_runs(self, limit: int = 5) -> list[dict]:
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sync_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_workout(self, workout: dict, synced_at: str) -> str:
        workout_id = workout.get("id")
        if not workout_id:
            return "ignored"
        workout_id = str(workout_id)
        incoming_time = parse_iso(workout.get("updated_at"))
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT source_time FROM workouts WHERE id = ?",
                (workout_id,),
            ).fetchone()
            if existing is not None and not _is_newer(incoming_time, parse_iso(existing["source_time"])):
                return "ignored"
            fields = {
                "id": workout_id,
                "title": workout.get("title"),
                "routine_id": workout.get("routine_id"),
                "description": workout.get("description"),
                "start_time": workout.get("start_time"),
                "end_time": workout.get("end_time"),
                "hevy_created_at": workout.get("created_at"),
                "hevy_updated_at": workout.get("updated_at"),
                "source_time": workout.get("updated_at"),
                "deleted_at": None,
                "raw_json": json.dumps(workout, ensure_ascii=False, sort_keys=True),
                "local_synced_at": synced_at,
            }
            if existing is None:
                action = "inserted"
                conn.execute(
                    """
                    INSERT INTO workouts (
                      id, title, routine_id, description, start_time, end_time,
                      hevy_created_at, hevy_updated_at, source_time, deleted_at,
                      raw_json, local_synced_at
                    ) VALUES (
                      :id, :title, :routine_id, :description, :start_time, :end_time,
                      :hevy_created_at, :hevy_updated_at, :source_time, :deleted_at,
                      :raw_json, :local_synced_at
                    )
                    """,
                    fields,
                )
            else:
                action = "updated"
                conn.execute(
                    """
                    UPDATE workouts SET
                      title = :title,
                      routine_id = :routine_id,
                      description = :description,
                      start_time = :start_time,
                      end_time = :end_time,
                      hevy_created_at = :hevy_created_at,
                      hevy_updated_at = :hevy_updated_at,
                      source_time = :source_time,
                      deleted_at = NULL,
                      raw_json = :raw_json,
                      local_synced_at = :local_synced_at
                    WHERE id = :id
                    """,
                    fields,
                )
            conn.execute("DELETE FROM exercises WHERE workout_id = ?", (workout_id,))
            for position, exercise in enumerate(workout.get("exercises") or []):
                exercise_index = _as_int(exercise.get("index"), position)
                conn.execute(
                    """
                    INSERT INTO exercises (
                      workout_id, exercise_index, title, notes, exercise_template_id, superset_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workout_id,
                        exercise_index,
                        exercise.get("title"),
                        exercise.get("notes"),
                        exercise.get("exercise_template_id"),
                        exercise.get("superset_id"),
                    ),
                )
                for set_position, entry in enumerate(exercise.get("sets") or []):
                    conn.execute(
                        """
                        INSERT INTO sets (
                          workout_id, exercise_index, set_index, set_type, weight_kg, reps,
                          distance_meters, duration_seconds, rpe, custom_metric
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workout_id,
                            exercise_index,
                            _as_int(entry.get("index"), set_position),
                            entry.get("type"),
                            entry.get("weight_kg"),
                            entry.get("reps"),
                            entry.get("distance_meters"),
                            entry.get("duration_seconds"),
                            entry.get("rpe"),
                            entry.get("custom_metric"),
                        ),
                    )
            return action

    def mark_deleted(self, workout_id: str, deleted_at: str | None, synced_at: str) -> str:
        event_time_text = deleted_at or synced_at
        incoming_time = parse_iso(event_time_text)
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT source_time FROM workouts WHERE id = ?",
                (workout_id,),
            ).fetchone()
            if existing is not None and not _is_newer(incoming_time, parse_iso(existing["source_time"])):
                return "ignored"
            raw = json.dumps(
                {"id": workout_id, "deleted_at": deleted_at, "type": "deleted"},
                sort_keys=True,
            )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO workouts (
                      id, raw_json, deleted_at, source_time, local_synced_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (workout_id, raw, event_time_text, event_time_text, synced_at),
                )
            else:
                conn.execute(
                    """
                    UPDATE workouts
                    SET deleted_at = ?, source_time = ?, local_synced_at = ?
                    WHERE id = ?
                    """,
                    (event_time_text, event_time_text, synced_at, workout_id),
                )
        return "deleted"

    def save_profile(self, user: dict, synced_at: str) -> None:
        weight_unit = user.get("weight_unit")
        distance_unit = user.get("distance_unit")
        if weight_unit not in {"kg", "lbs"}:
            weight_unit = None
        if distance_unit not in {"kilometers", "miles"}:
            distance_unit = None
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO profile (
                  id, hevy_user_id, username, display_name, weight_unit, distance_unit, synced_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  hevy_user_id = excluded.hevy_user_id,
                  username = excluded.username,
                  display_name = excluded.display_name,
                  weight_unit = excluded.weight_unit,
                  distance_unit = excluded.distance_unit,
                  synced_at = excluded.synced_at
                """,
                (
                    user.get("id"),
                    user.get("username"),
                    user.get("name"),
                    weight_unit,
                    distance_unit,
                    synced_at,
                ),
            )

    def start_run(self, started_at: str, mode: str) -> int:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE sync_state
                SET last_attempt_at = ?, last_mode = ?, last_error = NULL
                WHERE id = 1
                """,
                (started_at, mode),
            )
            cursor = conn.execute(
                """
                INSERT INTO sync_runs (started_at, mode, status)
                VALUES (?, ?, 'running')
                """,
                (started_at, mode),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        finished_at: str,
        status: str,
        upserted: int,
        deleted: int,
        ignored: int,
        error: str | None,
        warning: str | None,
        events_since: str | None,
        remote_count: int | None,
        local_count: int,
        advance_cursor: bool,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE sync_runs
                SET finished_at = ?, status = ?, workouts_upserted = ?, workouts_deleted = ?,
                    workouts_ignored = ?, error = ?
                WHERE id = ?
                """,
                (finished_at, status, upserted, deleted, ignored, error, run_id),
            )
            if advance_cursor:
                conn.execute(
                    """
                    UPDATE sync_state
                    SET last_success_at = ?,
                        last_error = NULL,
                        last_warning = ?,
                        events_since = ?,
                        remote_workout_count = ?,
                        local_workout_count = ?
                    WHERE id = 1
                    """,
                    (finished_at, warning, events_since, remote_count, local_count),
                )
            else:
                conn.execute(
                    """
                    UPDATE sync_state
                    SET last_error = ?,
                        last_warning = ?,
                        local_workout_count = ?
                    WHERE id = 1
                    """,
                    (error, warning, local_count),
                )

    def enqueue_webhook_job(self, workout_id: str, received_at: str) -> str:
        """Store one pending fetch per workout id. A repeat notice does not add another row."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM hevy_webhook_jobs WHERE workout_id = ?",
                (workout_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO hevy_webhook_jobs (
                      workout_id, status, attempts, received_at, updated_at, next_attempt_at, last_error
                    ) VALUES (?, 'pending', 0, ?, ?, NULL, NULL)
                    """,
                    (workout_id, received_at, received_at),
                )
                return "queued"
            if row["status"] == "failed":
                conn.execute(
                    """
                    UPDATE hevy_webhook_jobs
                    SET status = 'pending',
                        attempts = 0,
                        updated_at = ?,
                        next_attempt_at = NULL,
                        last_error = NULL
                    WHERE workout_id = ?
                    """,
                    (received_at, workout_id),
                )
                return "requeued"
            return "duplicate"

    def get_webhook_job(self, workout_id: str) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM hevy_webhook_jobs WHERE workout_id = ?",
                (workout_id,),
            ).fetchone()
        return dict(row) if row else None

    def webhook_counts(self) -> dict:
        counts = {status: 0 for status in ("pending", "running", "retry", "done", "failed")}
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM hevy_webhook_jobs GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        return counts

    def claim_due_webhook_jobs(self, now: str, limit: int = 5) -> list[str]:
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT workout_id FROM hevy_webhook_jobs
                WHERE status IN ('pending', 'retry')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY received_at, workout_id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            workout_ids = [str(row["workout_id"]) for row in rows]
            for workout_id in workout_ids:
                conn.execute(
                    """
                    UPDATE hevy_webhook_jobs
                    SET status = 'running', attempts = attempts + 1, updated_at = ?
                    WHERE workout_id = ?
                    """,
                    (now, workout_id),
                )
        return workout_ids

    def complete_webhook_job(
        self,
        workout_id: str,
        *,
        status: str,
        now: str,
        error: str | None,
        next_attempt_at: str | None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE hevy_webhook_jobs
                SET status = ?, updated_at = ?, last_error = ?, next_attempt_at = ?
                WHERE workout_id = ?
                """,
                (status, now, error, next_attempt_at, workout_id),
            )

    def recover_running_webhook_jobs(self, now: str) -> int:
        """A restart leaves an in-flight fetch pending so it can run again."""
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE hevy_webhook_jobs
                SET status = 'pending', updated_at = ?, next_attempt_at = NULL
                WHERE status = 'running'
                """,
                (now,),
            )
            return int(cursor.rowcount)

    def upsert_body_measurement(self, measurement: dict, synced_at: str) -> str:
        date = str(measurement.get("date") or "").strip()
        if len(date) != 10:
            return "ignored"
        columns = [
            "weight_kg",
            "lean_mass_kg",
            "fat_percent",
            "neck_cm",
            "shoulder_cm",
            "chest_cm",
            "left_bicep_cm",
            "right_bicep_cm",
            "left_forearm_cm",
            "right_forearm_cm",
            "abdomen",
            "waist",
            "hips",
            "left_thigh",
            "right_thigh",
            "left_calf",
            "right_calf",
        ]
        values = [measurement.get(column) for column in columns]
        with self.transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO body_measurements (
                  date, {", ".join(columns)}, raw_json, local_synced_at
                ) VALUES (
                  ?, {", ".join("?" for _ in columns)}, ?, ?
                )
                ON CONFLICT(date) DO UPDATE SET
                  {", ".join(f"{column} = excluded.{column}" for column in columns)},
                  raw_json = excluded.raw_json,
                  local_synced_at = excluded.local_synced_at
                """,
                (date, *values, json.dumps(measurement), synced_at),
            )
        return "upserted"

    def body_weight_history(self, limit: int = 30) -> list[dict]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT date, weight_kg FROM body_measurements "
                "WHERE weight_kg > 0 ORDER BY date DESC LIMIT ?",
                (max(1, min(limit, 365)),),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def latest_body_measurement(self) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM body_measurements ORDER BY date DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def get_body_entry(self) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM body_entry WHERE id = 1").fetchone()
        return dict(row) if row else None

    def save_body_entry(
        self,
        *,
        height_cm: float | None,
        age_years: int | None,
        sex: str | None,
        activity: str | None,
        updated_at: str,
        days_per_week: int | None = None,
        session_minutes: int | None = None,
        train_where: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO body_entry (
                  id, height_cm, age_years, sex, activity, days_per_week,
                  session_minutes, train_where, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  height_cm = excluded.height_cm,
                  age_years = excluded.age_years,
                  sex = excluded.sex,
                  activity = excluded.activity,
                  days_per_week = excluded.days_per_week,
                  session_minutes = excluded.session_minutes,
                  train_where = excluded.train_where,
                  updated_at = excluded.updated_at
                """,
                (
                    height_cm,
                    age_years,
                    sex,
                    activity,
                    days_per_week,
                    session_minutes,
                    train_where,
                    updated_at,
                ),
            )

    def save_goal(self, statement: str, created_at: str) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO goals (statement, created_at) VALUES (?, ?)",
                (statement, created_at),
            )
            return int(cursor.lastrowid)

    def list_goals(self) -> list[dict]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM goals ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_goal(self, goal_id: int) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
            return cursor.rowcount > 0

    def save_check_in(self, feeling: str, notes: str | None, created_at: str) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO check_ins (feeling, notes, created_at) VALUES (?, ?, ?)",
                (feeling, notes, created_at),
            )
            return int(cursor.lastrowid)

    def list_check_ins(self, limit: int = 30) -> list[dict]:
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM check_ins
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_check_in(self, check_in_id: int) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM check_ins WHERE id = ?", (check_in_id,))
            return cursor.rowcount > 0

    def active_workout_ids(self) -> set[str]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM workouts WHERE deleted_at IS NULL"
            ).fetchall()
        return {str(row["id"]) for row in rows}

    def confirmed_exercise_rows(self, start: str | None = None, end: str | None = None) -> list[dict]:
        """Sets from workouts Hevy still has. Deleted workouts are left out."""
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT
                  w.id AS workout_id,
                  w.title AS workout_title,
                  w.start_time,
                  e.exercise_index,
                  e.title AS exercise_title,
                  e.notes AS exercise_notes,
                  e.exercise_template_id,
                  s.set_index,
                  s.set_type,
                  s.weight_kg,
                  s.reps,
                  s.distance_meters,
                  s.duration_seconds,
                  s.rpe,
                  s.custom_metric
                FROM workouts w
                JOIN exercises e ON e.workout_id = w.id
                LEFT JOIN sets s
                  ON s.workout_id = e.workout_id AND s.exercise_index = e.exercise_index
                WHERE w.deleted_at IS NULL
                  AND (? IS NULL OR julianday(w.start_time) >= julianday(?))
                  AND (? IS NULL OR julianday(w.start_time) < julianday(?))
                ORDER BY w.start_time, w.id, e.exercise_index, s.set_index
                """,
                (start, start, end, end),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_coach_reply(self, created_at: str, status: str, question: str, body: dict) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO coach_replies (created_at, status, question, body_json)
                VALUES (?, ?, ?, ?)
                """,
                (created_at, status, question, json.dumps(body, ensure_ascii=False)),
            )
            conn.execute(
                """
                DELETE FROM coach_replies
                WHERE status NOT IN ('plan_draft', 'hevy_publication', 'plan_suggestion') AND id NOT IN (
                  SELECT id FROM coach_replies WHERE status NOT IN ('plan_draft', 'hevy_publication', 'plan_suggestion') ORDER BY id DESC LIMIT 20
                )
                """
            )

    def latest_coach_reply(self) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM coach_replies WHERE status NOT IN ('hevy_publication', 'plan_suggestion') ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        body = json.loads(row["body_json"])
        body["question"] = row["question"]
        body["status"] = row["status"]
        body["created_at"] = row["created_at"]
        body["reply_id"] = row["id"]
        return body

    def latest_training_plan(self) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM coach_replies WHERE status = 'plan_draft' ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {**json.loads(row["body_json"]), "created_at": row["created_at"], "status": row["status"]}

    def publication_events(self) -> list[dict]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT body_json FROM coach_replies WHERE status = 'hevy_publication' ORDER BY id").fetchall()
        return [json.loads(row["body_json"]) for row in rows]

    def plan_suggestion_events(self) -> list[dict]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT body_json FROM coach_replies WHERE status = 'plan_suggestion' ORDER BY id").fetchall()
        return [json.loads(row["body_json"]) for row in rows]

    def get_garmin_state(self):
        with self.transaction() as conn:
            row = conn.execute("SELECT body_json FROM garmin_state WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else {}

    def save_garmin_state(self, state):
        with self.transaction() as conn:
            conn.execute("INSERT INTO garmin_state (id, body_json) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET body_json=excluded.body_json", (json.dumps(state),))

    def upsert_garmin_days(self, rows, synced_at):
        with self.transaction() as conn:
            for row in rows:
                body = {**row, "synced_at": synced_at}
                conn.execute("INSERT INTO garmin_daily (date, body_json) VALUES (?, ?) ON CONFLICT(date) DO UPDATE SET body_json=excluded.body_json", (row["date"], json.dumps(body)))

    def list_garmin_days(self, limit=7):
        from app.garmin_data import SCALAR_FIELDS
        present = " OR ".join(f"json_extract(body_json, '$.{key}') IS NOT NULL" for key in SCALAR_FIELDS)
        with self.transaction() as conn:
            rows = conn.execute(f"SELECT body_json FROM garmin_daily WHERE {present} OR EXISTS (SELECT 1 FROM json_each(body_json, '$.sleep_scores')) ORDER BY date DESC LIMIT ?", (min(31, max(1, limit)),)).fetchall()
        return [json.loads(row[0]) for row in rows]


def _is_newer(incoming, existing) -> bool:
    """An event applies only when its timestamp is strictly newer than the stored one."""
    if existing is None:
        return True
    if incoming is None:
        return False
    return incoming > existing


def open_database(settings):
    """MongoDB is primary; SQLite is an explicit migration/rollback backend."""
    if settings.database_backend == "sqlite":
        return Database(settings.database_path)
    from app.mongo import MongoDatabase
    return MongoDatabase(
        settings.mongodb_uri, settings.mongodb_database,
        username=settings.mongodb_username, password=settings.mongodb_password,
        auth_source=settings.mongodb_auth_source,
    )
