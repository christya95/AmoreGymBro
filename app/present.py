"""Display helpers. Weights stay in kilograms in the database."""

KG_TO_LB = 2.2046226218
METERS_PER_MILE = 1609.344

HEVY_RECORD_NOTICE = (
    "These are saved Hevy workouts. A workout shows up here only after Hevy has saved it. "
    "Sets you are still logging in Hevy are not included, and this app cannot see them."
)
CONFIRMED_LABEL = "Confirmed Hevy record"
USER_REPORT_LABEL = "Your report"
USER_REPORT_NOTICE = (
    "This is a live report you typed. It is not a confirmed Hevy workout, and it is not sent to Hevy."
)
GOAL_NOTICE = "This is a goal you typed. It is not a confirmed Hevy workout."
HISTORY_NOTICE = (
    "This history is calculated from confirmed Hevy workouts saved in your private database. "
    "A set appears here only after Hevy has saved the workout."
)
VOLUME_NOTE = (
    "Volume is weight times reps on sets that are not warm-ups and that have both a weight and a rep count. "
    "Warm-ups are left out. A set missing weight or reps is not counted as zero."
)
RECORD_NOTE = "The personal record is the heaviest working-set weight, with the reps logged on that set."
FEELINGS = ("Fresh", "Okay", "Tired", "Sore", "Off")


def format_weight(weight_kg: float | None, unit: str | None) -> str:
    if weight_kg is None:
        return "—"
    if unit == "lbs":
        return f"{weight_kg * KG_TO_LB:.1f} lb"
    text = f"{weight_kg:.2f}".rstrip("0").rstrip(".")
    return f"{text} kg"


def format_distance(meters: float | None, unit: str | None) -> str | None:
    if meters is None:
        return None
    if unit == "miles":
        return f"{meters / METERS_PER_MILE:.2f} mi"
    return f"{meters / 1000:.2f} km"


def format_duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def volume_line(volume_kg: float | None, unit: str | None) -> str:
    if volume_kg is None:
        return "Not calculated"
    return format_weight(volume_kg, unit)


def record_line(record: dict | None, unit: str | None) -> str:
    if not record:
        return "No weighed working set is stored."
    weight = format_weight(record.get("weight_kg"), unit)
    reps = format_number(record.get("reps"))
    if reps is None:
        return f"{weight}, reps not logged"
    return f"{weight} × {reps}"


def format_number(value: float | None) -> str | None:
    if value is None:
        return None
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def prepare_exercises(exercises: list[dict], weight_unit: str | None, distance_unit: str | None) -> list[dict]:
    """Add display lines. Newest session first. Numbers stay the calculated ones."""
    prepared = []
    for exercise in exercises:
        sessions = []
        for session in reversed(exercise["sessions"]):
            sets = []
            for item in session["sets"]:
                sets.append({**item, "line": format_set(item, weight_unit, distance_unit)})
            sessions.append(
                {
                    **session,
                    "sets": sets,
                    "volume_line": volume_line(session.get("session_volume_kg"), weight_unit),
                }
            )
        prepared.append(
            {
                **exercise,
                "sessions": sessions,
                "volume_line": volume_line(exercise.get("total_volume_kg"), weight_unit),
                "record_line": record_line(exercise.get("personal_record"), weight_unit),
            }
        )
    return prepared


def format_set(entry: dict, weight_unit: str | None, distance_unit: str | None) -> str:
    parts: list[str] = []
    set_type = entry.get("set_type") or "normal"
    if set_type != "normal":
        parts.append(set_type)
    weight = format_weight(entry.get("weight_kg"), weight_unit)
    reps = format_number(entry.get("reps"))
    if entry.get("weight_kg") is not None or entry.get("reps") is not None:
        if entry.get("weight_kg") is not None and reps is not None:
            parts.append(f"{weight} × {reps}")
        elif entry.get("weight_kg") is not None:
            parts.append(weight)
        elif reps is not None:
            parts.append(f"{reps} reps")
    distance = format_distance(entry.get("distance_meters"), distance_unit)
    if distance:
        parts.append(distance)
    duration = format_duration(entry.get("duration_seconds"))
    if duration:
        parts.append(duration)
    if entry.get("rpe") is not None:
        parts.append(f"RPE {format_number(entry.get('rpe'))}")
    if entry.get("custom_metric") is not None:
        parts.append(f"metric {format_number(entry.get('custom_metric'))}")
    return " · ".join(parts) if parts else set_type
