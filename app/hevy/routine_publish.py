"""Explicitly approved first publication. Never exposed as a model tool.

POSTs are never retried automatically. Pending operations require reconciliation;
known routine IDs are reused/verified, never replaced with extra creations.
"""
from __future__ import annotations

import hashlib
import json
import os
import fcntl
from pathlib import Path
from datetime import datetime, timezone

import httpx

FOLDER = "AMORE GYM BRO"


class PublicationBlocked(Exception):
    pass


class RoutineAPI:
    def __init__(self, api_key):
        self.http = httpx.Client(base_url="https://api.hevyapp.com", headers={"api-key": api_key}, timeout=20)

    def close(self):
        self.http.close()

    def request(self, method, path, **kwargs):
        try:
            response = self.http.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception:
            raise PublicationBlocked("Hevy request did not complete. No automatic write retry; inspect the publication audit before continuing.") from None

    def all(self, path, key):
        rows = []
        for page in range(1, 1001):
            result = self.request("GET", path, params={"page": page, "pageSize": 10})
            if not isinstance(result.get(key), list) or "page_count" not in result:
                raise PublicationBlocked("Hevy listing was incomplete.")
            rows.extend(result[key])
            if page >= int(result["page_count"]):
                if len({row["id"] for row in rows}) != len(rows):
                    raise PublicationBlocked("Duplicate IDs in Hevy listing.")
                return rows
        raise PublicationBlocked("Hevy listing exceeded the safety limit.")


def prepare_payloads(plan, catalog):
    payloads = {}
    aliases = {"Treadmill walk or easy stationary bike": "Treadmill"}
    for day in plan["days"]:
        exercises = []
        for entry in day["exercises"]:
            name = aliases.get(entry["name"], entry["name"])
            matches = [item for item in catalog if item["title"] == name]
            if len(matches) != 1:
                raise PublicationBlocked("An exercise needs an unambiguous Hevy template mapping: " + name)
            template = matches[0]
            expected = "distance_duration" if entry["load_basis"] == "cardio" else "reps_only" if entry["load_basis"] == "bodyweight" else "weight_reps"
            if template["type"] != expected:
                raise PublicationBlocked("Hevy exercise type does not match the proposed prescription: " + name)
            note = entry["note"]
            if entry["load_basis"] == "per_dumbbell": note = "Weight is PER DUMBBELL, not the combined pair. " + note
            if entry["load_basis"] == "per_arm": note = "Complete both arms before marking a set complete; load is the stack setting for each arm. " + note
            reps = entry.get("reps_min")
            rep_range = None
            if reps is not None and reps != entry["reps_max"]:
                rep_range = {"start": reps, "end": entry["reps_max"]}
                reps = None
            item = {"type": "normal", "weight_kg": entry.get("weight_kg"), "reps": reps,
                    "rep_range": rep_range, "distance_meters": None,
                    "duration_seconds": entry["duration_minutes"] * 60 if entry.get("duration_minutes") else None,
                    "custom_metric": None}
            exercises.append({"exercise_template_id": template["id"], "superset_id": None,
                              "rest_seconds": entry["rest_seconds"], "notes": note,
                              "sets": [dict(item) for _ in range(entry["sets"])]})
        payloads[day["day"]] = {"title": day["day"][:3] + " · " + day["name"],
                                "notes": "AMORE GYM BRO | " + plan["plan_id"] + " | " + day["day"] + ". Trial loads for a gradual return, not tested current capacity. Warm up first; avoid failure.",
                                "exercises": exercises}
    return payloads


def _normalized(payload):
    def number(value):
        return round(float(value), 4) if value is not None else None
    return {"title": payload["title"], "folder_id": payload.get("folder_id"), "exercises": [
        {"id": entry["exercise_template_id"], "rest": entry.get("rest_seconds") or 0,
         "notes": entry.get("notes") or "", "sets": [
             {"type": item["type"], "weight": number(item.get("weight_kg")), "reps": item.get("reps"),
              "rep_range": item.get("rep_range"), "duration": item.get("duration_seconds"),
              "distance": item.get("distance_meters"), "custom": item.get("custom_metric")}
             for item in entry["sets"]]}
        for entry in payload["exercises"]]}


def _same(actual, expected):
    return _normalized(actual) == _normalized(expected)


def publication_summary(database, saved):
    if not saved:
        return {"complete": False, "routines": []}
    events = [event for event in database.publication_events() if event.get("plan_id") == saved["weekly_plan"]["plan_id"]]
    bound = {}
    for event in events:
        if event.get("event") == "verified" and event.get("plan_fingerprint") == saved["plan_fingerprint"]:
            bound[event["day"]] = {"day": event["day"], "id": event["routine_id"], "title": event["title"]}
    return {"complete": len(bound) == len(saved["weekly_plan"]["days"]), "routines": list(bound.values()), "folder": FOLDER}


def publish_initial(database, api, catalog, *, approved_fingerprint, approval_note):
    path = Path(os.environ.get("DATABASE_PATH", "./data/fitness.db")).parent / "hevy-publication.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        path.chmod(0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublicationBlocked("A publication is already in progress.") from None
        try:
            return _publish_locked(database, api, catalog, approved_fingerprint=approved_fingerprint, approval_note=approval_note)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _publish_locked(database, api, catalog, *, approved_fingerprint, approval_note):
    saved = database.latest_training_plan()
    if not saved or saved["plan_fingerprint"] != approved_fingerprint or not approval_note.strip():
        raise PublicationBlocked("This exact saved plan requires explicit human approval before publication.")
    payloads = prepare_payloads(saved["weekly_plan"], catalog)
    plan_id = saved["weekly_plan"]["plan_id"]
    events = [event for event in database.publication_events() if event.get("plan_id") == plan_id]
    if any(event.get("plan_fingerprint") != approved_fingerprint for event in events):
        raise PublicationBlocked("Existing bindings belong to another plan version. Use a separately reviewed update flow; do not create another set.")
    expected_days = {day["day"] for day in saved["weekly_plan"]["days"]}
    if set(payloads) != expected_days:
        raise PublicationBlocked("Publication does not match the saved plan days.")
    batch_hash = hashlib.sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()
    if any(event.get("batch_hash") != batch_hash for event in events):
        raise PublicationBlocked("Mapped payload changed after approval/publication began.")

    def audit(event, **details):
        now = datetime.now(timezone.utc).isoformat()
        record = {"event": event, "plan_id": plan_id, "plan_fingerprint": approved_fingerprint,
                  "version": saved["plan_version"], "batch_hash": batch_hash, "at": now, **details}
        database.save_coach_reply(now, "hevy_publication", "Approved Hevy routine publication", record)
        events.append(record)

    # Read complete lists before deciding whether any creation is needed.
    routines = api.all("/v1/routines", "routines")
    folders = api.all("/v1/routine_folders", "routine_folders")
    candidates = [folder for folder in folders if folder["title"] == FOLDER]
    if len(candidates) > 1:
        raise PublicationBlocked("Multiple matching folders; select one explicitly before writing.")
    if not events:
        audit("approved", approval=approval_note, existing_routine_ids=[item["id"] for item in routines])
    if not candidates:
        if any(event["event"] == "folder_confirmed" for event in events):
            raise PublicationBlocked("The bound folder is missing. Do not silently recreate it.")
        if any(event["event"] == "folder_pending" for event in events):
            raise PublicationBlocked("An earlier folder creation is unresolved; do not repeat it.")
        audit("folder_pending")
        created = api.request("POST", "/v1/routine_folders", json={"routine_folder": {"title": FOLDER}})
        folder = created.get("routine_folder", created)
        if not isinstance(folder, dict) or not isinstance(folder.get("id"), int):
            raise PublicationBlocked("Folder creation outcome is uncertain; reconcile before retrying.")
        folder_id = folder["id"]
        audit("folder_confirmed", folder_id=folder_id)
    else:
        folder_id = candidates[0]["id"]
        known = [event["folder_id"] for event in events if event["event"] == "folder_confirmed"]
        if known and known[-1] != folder_id:
            raise PublicationBlocked("The bound folder disappeared or changed. Do not silently replace it.")
        if not known: audit("folder_confirmed", folder_id=folder_id)

    # Reverse creation order puts Monday last-created on Hevy's common newest-first view.
    for day, base in reversed(list(payloads.items())):
        expected = {**base, "folder_id": folder_id}
        bindings = [event for event in events if event.get("day") == day and event["event"] in {"created", "verified"}]
        if bindings:
            routine_id = bindings[-1]["routine_id"]
        else:
            matches = [row for row in routines if row.get("folder_id") == folder_id and row["title"] == expected["title"]]
            pending = any(event.get("day") == day and event["event"] == "create_pending" for event in events)
            if pending:
                if len(matches) != 1 or not _same(matches[0], expected):
                    raise PublicationBlocked("A previous routine creation needs reconciliation; no duplicate will be created.")
                routine_id = matches[0]["id"]
                audit("created", day=day, routine_id=routine_id, reconciled=True)
            elif matches:
                raise PublicationBlocked("An unlinked routine already has the target title. Review before linking or creating.")
            else:
                audit("create_pending", day=day, title=expected["title"], payload=expected)
                created = api.request("POST", "/v1/routines", json={"routine": expected})
                routine = created.get("routine", created)
                if not isinstance(routine, dict) or not isinstance(routine.get("id"), str):
                    raise PublicationBlocked("Routine creation outcome is uncertain; reconcile rather than repeating the POST.")
                routine_id = routine["id"]
                audit("created", day=day, routine_id=routine_id)
        actual = api.request("GET", "/v1/routines/" + routine_id)
        actual = actual.get("routine", actual)
        if not _same(actual, expected):
            raise PublicationBlocked("Hevy's saved routine differs from the approved payload. Inspect it; no overwrite or duplicate was attempted.")
        if not any(event.get("day") == day and event["event"] == "verified" for event in events):
            audit("verified", day=day, routine_id=routine_id, title=expected["title"])
    return publication_summary(database, saved)
