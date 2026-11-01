"""Reviewed updates of bound Hevy routines; never a model tool or a creator.

Keep the review (especially ``entries``) on the server. Only ``changes`` is UI
copy; a submitted fingerprint is approval, not a client-supplied write payload.
Preview is read-only, including reconciliation of an uncertain earlier PUT.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

from app.hevy.routine_publish import (
    PublicationBlocked, _normalized, _same, prepare_payloads, publication_summary,
)
from app.present import format_weight

__all__ = ["PublicationBlocked", "prepare_update_review", "apply_update_review"]


def _snapshot(payload):
    # Ignore response metadata (timestamps, indexes, exercise display titles),
    # and unsupported parent notes, which GET does not expose. Exercise notes
    # and supersets remain protected along with _same's prescriptions.
    try:
        content = {**_normalized(payload),
                   "supersets": [entry.get("superset_id") for entry in payload["exercises"]]}
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PublicationBlocked("A routine payload is incomplete; review it before updating.") from None


def _matches(actual, expected):
    return _snapshot(actual) == _snapshot(expected) and _same(actual, expected)


def _bindings(database, saved):
    events = [event for event in database.publication_events()
              if event.get("plan_id") == saved["weekly_plan"]["plan_id"]]
    bindings = {}
    for index, event in enumerate(events):
        if event.get("event") == "verified":
            bindings[event["day"]] = (index, event)
    result = {}
    for day in saved["weekly_plan"]["days"]:
        name = day["day"]
        if name not in bindings:
            raise PublicationBlocked("No verified saved routine binding for " + name + ". Publish or review it separately.")
        index, verified = bindings[name]
        routine_id = verified.get("routine_id")
        if not isinstance(routine_id, str) or not routine_id:
            raise PublicationBlocked("The saved routine ID is missing for " + name + ".")
        baseline = verified.get("payload")
        if baseline is None:
            candidates = [event for event in events[:index] if event.get("event") == "create_pending"
                          and event.get("day") == name
                          and event.get("plan_fingerprint") == verified.get("plan_fingerprint")]
            baseline = candidates[-1].get("payload") if candidates else None
        if not isinstance(baseline, dict) or baseline.get("folder_id") is None:
            raise PublicationBlocked("The verified baseline or folder is missing for " + name + ".")
        _snapshot(baseline)
        pending = [event for event in events[index + 1:] if event.get("event") == "update_pending"
                   and (event.get("routine_id") == routine_id or event.get("day") == name)]
        result[name] = {"routine_id": routine_id, "baseline": deepcopy(baseline),
                        "verified": verified, "pending": pending}
    if len({item["routine_id"] for item in result.values()}) != len(result):
        raise PublicationBlocked("Multiple training days share a saved routine ID; review the bindings separately.")
    return result


def _request(api, method, routine_id, **kwargs):
    try:
        return api.request(method, "/v1/routines/" + quote(routine_id, safe=""), **kwargs)
    except Exception:
        raise PublicationBlocked(
            "Hevy request did not complete. No automatic write retry; review the update audit before continuing."
        ) from None


def _current(api, routine_id):
    actual = _request(api, "GET", routine_id)
    if isinstance(actual, dict):
        actual = actual.get("routine", actual)
    if not isinstance(actual, dict) or actual.get("id", routine_id) != routine_id:
        raise PublicationBlocked("The bound routine is missing or returned another ID; no replacement will be created.")
    _snapshot(actual)
    return actual


def _checked_entry(api, saved, day, binding, desired):
    baseline = binding["baseline"]
    if "notes" in desired:
        raise PublicationBlocked("Routine-level notes are unsupported; prepare a new review without that field.")
    if desired.get("folder_id") != baseline["folder_id"]:
        raise PublicationBlocked("The reviewed routine must preserve its saved folder.")
    actual = _current(api, binding["routine_id"])
    pending = binding["pending"]
    if pending:
        # An uncertain write is never retried, even if GET still shows baseline.
        # Only the exact intended result of this plan may be reconciled.
        if not all(event.get("plan_fingerprint") == saved["plan_fingerprint"]
                   and event.get("routine_id") == binding["routine_id"]
                   and event.get("day") == day
                   and isinstance(event.get("payload"), dict)
                   and _matches(event["payload"], desired) for event in pending) or not _matches(actual, desired):
            raise PublicationBlocked("An earlier update is unresolved for " + day + "; reconcile it before another write.")
    elif not _matches(actual, baseline):
        raise PublicationBlocked("The saved Hevy routine was manually edited for " + day + "; review those edits before updating.")
    return {"day": day, "routine_id": binding["routine_id"], "desired": deepcopy(desired),
            "baseline": deepcopy(baseline), "current_snapshot": _snapshot(actual),
            "changed": not _matches(actual, desired), "reconciled": bool(pending)}


def _display(payload, titles, unit):
    lines = [payload["title"]]
    for exercise in payload["exercises"]:
        name = titles.get(exercise["exercise_template_id"], "Previously saved exercise")
        sets = exercise["sets"]
        descriptions = []
        for item in sets:
            parts = []
            if item.get("rep_range") is not None:
                rep_range = item["rep_range"]
                parts.append(f"{rep_range['start']}–{rep_range['end']} reps")
            elif item.get("reps") is not None:
                parts.append(f"{item['reps']} reps")
            if item.get("weight_kg") is not None:
                parts.append(format_weight(item["weight_kg"], unit))
            if item.get("duration_seconds") is not None:
                parts.append(f"{item['duration_seconds'] / 60:g} min")
            if item.get("distance_meters") is not None:
                parts.append(f"{item['distance_meters']:g} m")
            if item.get("type", "normal") != "normal":
                parts.append(item["type"] + " set")
            if item.get("custom_metric") is not None:
                parts.append(f"Metric: {item['custom_metric']}")
            descriptions.append(" · ".join(parts) or "Prescription not specified")
        if descriptions and len(set(descriptions)) == 1:
            lines.append(f"{name}: {len(sets)} {'set' if len(sets) == 1 else 'sets'} × {descriptions[0]}")
        else:
            lines.append(f"{name}: {len(sets)} sets")
            lines.extend(f"Set {index}: {text}" for index, text in enumerate(descriptions, 1))
        lines.append(f"Rest: {exercise.get('rest_seconds') or 0} seconds")
        if exercise.get("superset_id") is not None:
            lines.append(f"Superset group: {exercise['superset_id']}")
        if exercise.get("notes"):
            lines.append(exercise["notes"])
    return lines


def prepare_update_review(database, api, catalog, unit) -> dict:
    """Read current bound routines and return a private, approval-ready review.

    Unsafe/missing bindings, manual edits and unresolved writes raise
    PublicationBlocked. No database or remote writes occur here.
    ``already_current`` is the existing historical audit summary; use
    ``can_approve`` to decide whether this version needs an approval action.
    """
    saved = database.latest_training_plan()
    if not saved:
        raise PublicationBlocked("Save a weekly plan before reviewing Hevy updates.")
    payloads = prepare_payloads(saved["weekly_plan"], catalog)
    bindings = _bindings(database, saved)
    entries, changes = [], []
    titles = {item["id"]: item["title"] for item in catalog}
    for day, payload in payloads.items():
        binding = bindings[day]
        desired = {**payload, "folder_id": binding["baseline"]["folder_id"]}
        # The initial creator's audit contains parent notes, but GET omits
        # them. Do not display them as saved facts or send this unsupported
        # field in an update. Exercise notes are supported and stay intact.
        desired.pop("notes", None)
        entry = _checked_entry(api, saved, day, binding, desired)
        entries.append(entry)
        # Reconciled entries already contain the desired prescription remotely.
        changes.append({"day": day, "title": desired["title"],
                        "before": _display(desired if entry["reconciled"] else binding["baseline"], titles, unit),
                        "after": _display(desired, titles, unit), "changed": entry["changed"]})
    count = sum(entry["changed"] for entry in entries)
    already_current = publication_summary(database, saved)["complete"]
    # publication_summary is a historical fingerprint projection. Returning to
    # an older fingerprint still needs approval/verification for this version.
    needs_verification = any(binding["pending"]
                             or binding["verified"].get("plan_fingerprint") != saved["plan_fingerprint"]
                             or binding["verified"].get("version") != saved["plan_version"]
                             for binding in bindings.values())
    return {"plan_fingerprint": saved["plan_fingerprint"], "plan_version": saved["plan_version"],
            "plan_id": saved["weekly_plan"]["plan_id"], "entries": entries, "changes": changes,
            "changes_count": count, "already_current": already_current,
            "can_approve": bool(count or needs_verification or not already_current)}


def apply_update_review(database, api, review, *, approved_fingerprint, approval_note):
    """Apply a server-held review only after explicit approval of its saved plan."""
    # Exactly the same lock path and locking protocol as publish_initial.
    path = Path(os.environ.get("DATABASE_PATH", "./data/fitness.db")).parent / "hevy-publication.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        path.chmod(0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublicationBlocked("A publication is already in progress.") from None
        try:
            return _apply_locked(database, api, review, approved_fingerprint, approval_note)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _require_approval(database, review, fingerprint, note):
    saved = database.latest_training_plan()
    if (not saved or not isinstance(note, str) or not note.strip()
            or saved["plan_fingerprint"] != fingerprint
            or review.get("plan_fingerprint") != fingerprint
            or review.get("plan_version") != saved["plan_version"]
            or review.get("plan_id") != saved["weekly_plan"]["plan_id"]):
        raise PublicationBlocked("This exact latest saved plan and review require explicit human approval before updating.")
    return saved


def _apply_locked(database, api, review, fingerprint, note):
    saved = _require_approval(database, review, fingerprint, note)
    bindings = _bindings(database, saved)
    entries = review.get("entries", [])
    if (not isinstance(entries, list) or len(entries) != len(bindings)
            or {entry["day"] for entry in entries} != set(bindings)):
        raise PublicationBlocked("The review does not cover every saved training day.")
    checked = []
    # Complete every preflight GET before an approval audit or any PUT.
    for entry in entries:
        binding = bindings[entry["day"]]
        if (entry["routine_id"] != binding["routine_id"]
                or not _matches(entry["baseline"], binding["baseline"])):
            raise PublicationBlocked("The saved binding changed after review; prepare a new review.")
        fresh = _checked_entry(api, saved, entry["day"], binding, entry["desired"])
        if fresh["current_snapshot"] != entry["current_snapshot"] or fresh["changed"] != entry["changed"]:
            raise PublicationBlocked("A Hevy routine snapshot changed after review; prepare a new review.")
        checked.append(fresh)
    _require_approval(database, review, fingerprint, note)

    def audit(event, **details):
        now = datetime.now(timezone.utc).isoformat()
        record = {"event": event, "plan_id": saved["weekly_plan"]["plan_id"],
                  "plan_fingerprint": fingerprint, "version": saved["plan_version"], "at": now, **details}
        database.save_coach_reply(now, "hevy_publication", "Approved Hevy routine update", record)

    audit("update_approved", approval=note, changes_count=sum(entry["changed"] for entry in checked),
          routines=[{"day": entry["day"], "routine_id": entry["routine_id"],
                     "current_snapshot": entry["current_snapshot"]} for entry in checked])
    updated = 0
    for entry in checked:
        # Plan edits do not take the publication lock. Stop if one arrives while
        # this batch is running, retaining any completed/pending audit records.
        _require_approval(database, review, fingerprint, note)
        day, routine_id, desired = entry["day"], entry["routine_id"], entry["desired"]
        if entry["changed"]:
            audit("update_pending", day=day, routine_id=routine_id, title=desired["title"],
                  payload=desired, baseline=entry["baseline"], current_snapshot=entry["current_snapshot"])
            _request(api, "PUT", routine_id, json={"routine": deepcopy(desired)})
            actual = _current(api, routine_id)
            if not _matches(actual, desired):
                raise PublicationBlocked("Hevy's saved routine differs from the approved update; reconcile the pending write before continuing.")
            updated += 1
        verified = bindings[day]["verified"]
        if (entry["changed"] or entry["reconciled"] or verified.get("plan_fingerprint") != fingerprint
                or verified.get("version") != saved["plan_version"]):
            audit("verified", day=day, routine_id=routine_id, title=desired["title"], payload=desired,
                  reconciled=entry["reconciled"])
    summary = publication_summary(database, saved)
    if not summary["complete"]:
        raise PublicationBlocked("The update audit is incomplete; review the saved bindings before continuing.")
    return {**summary, "updated_count": updated}
