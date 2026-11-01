"""Read-only routine library; never contributes to completed-workout metrics."""
from app.hevy.client import HevyError
from app.present import format_number, format_set


def _routine_set_line(entry, weight_unit, distance_unit):
    if not isinstance(entry, dict):
        raise HevyError("Invalid routine set.")
    rep_range = entry.get("rep_range")
    # Routine ranges replace scalar reps; neither is completed performance.
    value = {
        **entry,
        "set_type": entry.get("type"),
        "reps": None if rep_range else entry.get("reps"),
    }
    line = format_set(value, weight_unit, distance_unit)
    if rep_range:
        start, end = rep_range.get("start"), rep_range.get("end")
        reps = f"{format_number(start) or '—'}–{format_number(end) or '—'} reps"
        line = reps if line == "normal" else f"{line} · {reps}"
    return line if line != "normal" else "Targets not set"


def routine_library(client, weight_unit=None, distance_unit=None):
    # Consume every page before exposing a successful list. Failed later pages
    # must not look like an empty account or an apparently complete first page.
    folders = list(client.iter_routine_folders())
    routines = list(client.iter_routines())
    if any(not isinstance(f, dict) or f.get("id") is None for f in folders):
        raise HevyError("Invalid routine folders.")
    folder_names = {str(f["id"]): f.get("title") or "Untitled folder" for f in folders}
    unique = {}
    for routine in routines:
        if not isinstance(routine, dict) or not isinstance(routine.get("id"), str) or not routine["id"]:
            raise HevyError("Invalid routine.")
        exercises = routine.get("exercises")
        if not isinstance(exercises, list):
            raise HevyError("Invalid routine exercises.")
        prepared = []
        for exercise in exercises:
            if not isinstance(exercise, dict) or not isinstance(exercise.get("sets"), list):
                raise HevyError("Invalid routine sets.")
            lines = [
                _routine_set_line(entry, weight_unit, distance_unit)
                for entry in exercise["sets"]
            ]
            prepared.append({"title": exercise.get("title") or "Untitled exercise", "sets": lines,
                "notes": exercise.get("notes"), "rest_seconds": exercise.get("rest_seconds"),
                "superset_id": exercise.get("superset_id")})
        folder_id = routine.get("folder_id")
        unique[routine["id"]] = {"title": routine.get("title") or "Untitled routine",
            "folder": "Unfiled" if folder_id is None else folder_names.get(str(folder_id), "Folder name unavailable"),
            "updated_at": routine.get("updated_at"), "exercises": prepared}
    return sorted(unique.values(), key=lambda r: (r["folder"].casefold(), r["title"].casefold()))
