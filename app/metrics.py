"""Training totals from confirmed Hevy rows. This module does not guess missing loads."""

from __future__ import annotations


def exercise_key(template_id: str | None, title: str | None) -> str:
    if template_id:
        return f"id:{template_id}"
    return f"title:{title or 'Exercise'}"


def summarize(rows: list[dict], workout_count: int) -> dict:
    """Volume, counts, and per-exercise records for active confirmed workouts."""
    exercises = _exercises(rows)
    totals = _totals(rows, workout_count)
    return {"totals": totals, "exercises": exercises}


def information_gaps(
    totals: dict,
    *,
    goal_count: int,
    check_in_count: int,
    last_success_at: str | None,
    include_profile: bool = True,
) -> list[str]:
    gaps: list[str] = []
    if totals["confirmed_workout_count"] == 0:
        gaps.append("No confirmed Hevy workouts are stored.")
    elif totals["total_volume_kg"] is None:
        gaps.append("No working set has both weight and reps, so volume was not calculated.")
    if totals["sets_missing_load"]:
        count = totals["sets_missing_load"]
        if count == 1:
            gaps.append(
                "1 working set is missing a weight or a rep count and was left out of the volume."
            )
        else:
            gaps.append(
                f"{count} working sets are missing a weight or a rep count and were left out of the volume."
            )
    if include_profile and goal_count == 0:
        gaps.append("No training goal is saved.")
    if include_profile and check_in_count == 0:
        gaps.append("No check-in is saved.")
    if not last_success_at:
        gaps.append("The Hevy copy has not succeeded yet.")
    return gaps


def _totals(rows: list[dict], workout_count: int) -> dict:
    working = 0
    warmups = 0
    missing = 0
    volume = 0.0
    volume_sets = 0
    distance = 0.0
    distance_sets = 0
    duration = 0.0
    duration_sets = 0
    for row in rows:
        if row.get("set_index") is None:
            continue
        if _is_warmup(row.get("set_type")):
            warmups += 1
            continue
        working += 1
        set_volume = _set_volume(row.get("weight_kg"), row.get("reps"))
        if set_volume is None:
            missing += 1
        else:
            volume += set_volume
            volume_sets += 1
        if row.get("distance_meters") is not None:
            distance += float(row["distance_meters"])
            distance_sets += 1
        if row.get("duration_seconds") is not None:
            duration += float(row["duration_seconds"])
            duration_sets += 1
    return {
        "confirmed_workout_count": workout_count,
        "working_set_count": working,
        "warmup_set_count": warmups,
        "sets_with_volume": volume_sets,
        "sets_missing_load": missing,
        "total_volume_kg": _round(volume) if volume_sets else None,
        "total_distance_meters": _round(distance) if distance_sets else None,
        "total_duration_seconds": _round(duration) if duration_sets else None,
    }


def _exercises(rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        title = row.get("exercise_title") or "Exercise"
        key = exercise_key(row.get("exercise_template_id"), title)
        exercise = grouped.get(key)
        if exercise is None:
            exercise = {
                "key": key,
                "title": title,
                "exercise_template_id": row.get("exercise_template_id"),
                "sessions": {},
            }
            grouped[key] = exercise
        workout_id = str(row["workout_id"])
        session = exercise["sessions"].get(workout_id)
        if session is None:
            session = {
                "workout_id": workout_id,
                "workout_title": row.get("workout_title"),
                "start_time": row.get("start_time"),
                "untrusted_note": _clip(row.get("exercise_notes")),
                "sets": [],
            }
            exercise["sessions"][workout_id] = session
        elif row.get("exercise_notes") and not session["untrusted_note"]:
            session["untrusted_note"] = _clip(row.get("exercise_notes"))
        if row.get("set_index") is None:
            continue
        weight = _optional_float(row.get("weight_kg"))
        reps = _optional_float(row.get("reps"))
        session["sets"].append(
            {
                "set_index": int(row["set_index"]),
                "set_type": row.get("set_type") or "normal",
                "weight_kg": _round(weight) if weight is not None else None,
                "reps": _round(reps) if reps is not None else None,
                "volume_kg": _set_volume(weight, reps) if not _is_warmup(row.get("set_type")) else None,
                "distance_meters": _optional_round(row.get("distance_meters")),
                "duration_seconds": _optional_round(row.get("duration_seconds")),
                "rpe": _optional_round(row.get("rpe")),
                "custom_metric": _optional_round(row.get("custom_metric")),
            }
        )

    exercises = []
    for exercise in grouped.values():
        sessions = sorted(
            exercise["sessions"].values(),
            key=lambda item: (item.get("start_time") or "", item["workout_id"]),
        )
        for session in sessions:
            session["sets"].sort(key=lambda item: item["set_index"])
            volumes = [item["volume_kg"] for item in session["sets"] if item["volume_kg"] is not None]
            session["session_volume_kg"] = _round(sum(volumes)) if volumes else None
            session["sets_missing_load"] = sum(
                1
                for item in session["sets"]
                if not _is_warmup(item["set_type"]) and item["volume_kg"] is None
            )
        working_sets = [
            item
            for session in sessions
            for item in session["sets"]
            if not _is_warmup(item["set_type"])
        ]
        weighed = [item for item in working_sets if item["weight_kg"] is not None]
        volumes = [item["volume_kg"] for item in working_sets if item["volume_kg"] is not None]
        record = _personal_record(sessions) if weighed else None
        exercises.append(
            {
                "key": exercise["key"],
                "title": exercise["title"],
                "exercise_template_id": exercise["exercise_template_id"],
                "session_count": len(sessions),
                "working_set_count": len(working_sets),
                "sets_missing_load": sum(1 for item in working_sets if item["volume_kg"] is None),
                "total_volume_kg": _round(sum(volumes)) if volumes else None,
                "personal_record": record,
                "sessions": sessions,
            }
        )
    exercises.sort(
        key=lambda item: (
            max((session.get("start_time") or "" for session in item["sessions"]), default=""),
            item["title"],
        ),
        reverse=True,
    )
    return exercises


def _personal_record(sessions: list[dict]) -> dict | None:
    best = None
    best_key = None
    for session in sessions:
        for item in session["sets"]:
            if _is_warmup(item["set_type"]) or item["weight_kg"] is None:
                continue
            reps = item["reps"] if item["reps"] is not None else -1
            # Heaviest weight, then more reps, then the earlier time that hit it.
            key = (item["weight_kg"], reps, _invert_time(session.get("start_time")))
            if best_key is None or key > best_key:
                best_key = key
                best = {
                    "weight_kg": item["weight_kg"],
                    "reps": item["reps"],
                    "workout_id": session["workout_id"],
                    "start_time": session.get("start_time"),
                }
    return best


def _invert_time(value: str | None) -> str:
    if not value:
        return ""
    return "".join(chr(255 - ord(char)) for char in value)


def _is_warmup(set_type: str | None) -> bool:
    return (set_type or "normal") == "warmup"


def _set_volume(weight, reps) -> float | None:
    if weight is None or reps is None:
        return None
    return _round(float(weight) * float(reps))


def _optional_float(value) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_round(value) -> float | None:
    number = _optional_float(value)
    if number is None:
        return None
    return _round(number)


def _round(value: float) -> float:
    return round(float(value), 2)


def _clip(value: str | None, limit: int = 400) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text
