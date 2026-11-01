"""Update safety contracts using only temporary SQLite and an in-memory Hevy."""
from copy import deepcopy
import fcntl
import json

import pytest

from app.db import Database
from app.hevy.routine_publish import publish_initial, publication_summary
from app.hevy.routine_update import PublicationBlocked, apply_update_review, prepare_update_review
from app.training_plan import WeeklyPlan, save_plan_proposal


class FakeAPI:
    def __init__(self, database):
        self.database = database
        self.folders = []
        self.routines = {}
        self.calls = []
        self.fail_put = None
        self.audit_at_put = []
        self.put_payloads = []

    def all(self, path, key):
        self.calls.append(("GET", path))
        return deepcopy(self.folders if key == "routine_folders" else list(self.routines.values()))

    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if method == "POST":
            if path == "/v1/routine_folders":
                row = {"id": 123, **deepcopy(kwargs["json"]["routine_folder"])}
                self.folders.append(row)
                return {"routine_folder": deepcopy(row)}
            row = {"id": "saved-" + str(len(self.routines)), **deepcopy(kwargs["json"]["routine"])}
            self.routines[row["id"]] = row
            return {"routine": deepcopy(row)}
        routine_id = path.rsplit("/", 1)[-1]
        if routine_id not in self.routines:
            raise PublicationBlocked("Missing routine")
        if method == "PUT":
            self.put_payloads.append(deepcopy(kwargs["json"]["routine"]))
            self.audit_at_put.append(deepcopy(self.database.publication_events()))
            if self.fail_put == "before":
                raise TimeoutError("Request outcome unknown")
            self.routines[routine_id] = {"id": routine_id, **deepcopy(kwargs["json"]["routine"])}
            if self.fail_put == "after":
                self.fail_put = None
                raise TimeoutError("Response lost after PUT was saved")
            if self.fail_put == "mismatch":
                self.routines[routine_id]["exercises"][0]["sets"][0]["reps"] = 1
            return {"routine": deepcopy(self.routines[routine_id])}
        assert method == "GET", "Updates may never delete or create replacements"
        routine = deepcopy(self.routines[routine_id])
        routine.pop("notes", None)  # Official GET does not expose parent notes.
        return {"routine": routine}

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


def plan():
    exercise = {"name": "Machine press", "sets": 2, "reps_min": 8, "reps_max": 12,
                "weight_kg": 10, "load_basis": "machine_display", "rest_seconds": 120,
                "note": "Conservative proposed trial load.", "reference": "Earlier saved set"}
    return {"plan_id": "update-test", "name": "Proposed week", "goal": "Rebuild strength",
            "phase": "Return phase", "intake": ["User report: returning to lifting"],
            "rules": ["Avoid failure"],
            "sources": [{"title": "Training guidance", "url": "https://pubmed.ncbi.nlm.nih.gov/41843416/"}],
            "days": [{"day": "Monday", "name": "Upper", "focus": "Upper body", "exercises": [deepcopy(exercise)]},
                     {"day": "Thursday", "name": "Repeat", "focus": "Upper body", "exercises": [deepcopy(exercise)]}]}


def save(database, payload):
    return save_plan_proposal(database, WeeklyPlan.model_validate(payload), "2026-11-01T12:00:00Z")


@pytest.fixture
def published(tmp_path, monkeypatch):
    path = tmp_path / "data" / "fitness.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    database = Database(str(path))
    database.migrate()
    saved = save(database, plan())
    catalog = [{"id": "press", "title": "Machine press", "type": "weight_reps"}]
    api = FakeAPI(database)
    assert publish_initial(database, api, catalog, approved_fingerprint=saved["plan_fingerprint"],
                           approval_note="Human approved initial publication")["complete"]
    api.calls.clear()
    return database, saved, catalog, api


def changed_review(published, *, both=False):
    database, _, catalog, api = published
    payload = plan()
    for day in payload["days"] if both else payload["days"][:1]:
        day["exercises"][0]["weight_kg"] = 15
    saved = save(database, payload)
    return saved, prepare_update_review(database, api, catalog, "kg")


def approve(database, api, review, note="Human approved the displayed changes"):
    return apply_update_review(database, api, review, approved_fingerprint=review["plan_fingerprint"], approval_note=note)


def test_preview_is_read_only_and_human_readable(published):
    database, _, catalog, api = published
    saved, review = changed_review(published)
    before_events = deepcopy(database.publication_events())
    before_routines = deepcopy(api.routines)
    api.calls.clear()
    review = prepare_update_review(database, api, catalog, "lbs")
    assert database.publication_events() == before_events
    assert api.routines == before_routines and api.writes == []
    assert len(api.calls) == 2 and all(method == "GET" for method, _ in api.calls)
    assert review["plan_fingerprint"] == saved["plan_fingerprint"]
    assert review["plan_version"] == 2 and review["plan_id"] == "update-test"
    assert review["changes_count"] == 1 and not review["already_current"] and review["can_approve"]
    change = review["changes"][0]
    assert change["day"] == "Monday" and change["changed"]
    assert "Machine press: 2 sets × 8–12 reps · 22.0 lb" in change["before"]
    assert "Machine press: 2 sets × 8–12 reps · 33.1 lb" in change["after"]
    assert all(isinstance(line, str) and "weight_kg" not in line and "exercise_template_id" not in line
               for diff in review["changes"] for line in diff["before"] + diff["after"])
    assert all(len(entry["current_snapshot"]) == 64 for entry in review["entries"])


def test_updates_same_ids_and_audits_before_put_with_all_snapshots_checked(published):
    database, _, catalog, api = published
    saved, review = changed_review(published, both=True)
    original_ids = set(api.routines)
    api.calls.clear()
    result = approve(database, api, review)
    assert result["complete"] and result["updated_count"] == 2
    assert set(api.routines) == original_ids
    assert api.calls[:2] == [("GET", "/v1/routines/" + entry["routine_id"]) for entry in review["entries"]]
    assert api.writes == [("PUT", "/v1/routines/" + entry["routine_id"]) for entry in review["entries"]]
    assert all(row["folder_id"] == 123 for row in api.routines.values())
    for events in api.audit_at_put:
        assert events[-1]["event"] == "update_pending"
        assert any(event["event"] == "update_approved" and event["approval"] for event in events)
        pending = events[-1]
        assert pending["plan_fingerprint"] == saved["plan_fingerprint"] and pending["version"] == 2
        assert pending["payload"]["exercises"][0]["sets"][0]["weight_kg"] == 15
        assert pending["baseline"]["exercises"][0]["sets"][0]["weight_kg"] == 10
    events = database.publication_events()
    verified = [event for event in events if event["event"] == "verified" and event["version"] == 2]
    assert len(verified) == 2
    assert all(event["payload"]["folder_id"] == 123 for event in verified)
    assert publication_summary(database, saved)["complete"]
    fresh = prepare_update_review(database, api, catalog, "kg")
    assert fresh["already_current"] and fresh["changes_count"] == 0 and not fresh["can_approve"]


def test_unchanged_new_version_requires_approval_and_never_puts(published):
    database, _, catalog, api = published
    payload = plan()
    payload["goal"] = "Same prescriptions, revised goal wording"
    saved = save(database, payload)
    events_before = deepcopy(database.publication_events())
    review = prepare_update_review(database, api, catalog, "kg")
    assert review["changes_count"] == 0 and review["can_approve"] and not review["already_current"]
    assert database.publication_events() == events_before
    assert not publication_summary(database, saved)["complete"]
    assert approve(database, api, review) == {**publication_summary(database, saved), "updated_count": 0}
    assert publication_summary(database, saved)["complete"] and api.writes == []


def test_unchanged_days_are_verified_in_the_new_version_without_put(published):
    database, _, _, api = published
    saved, review = changed_review(published)
    assert approve(database, api, review)["updated_count"] == 1
    assert len(api.writes) == 1
    verified = [event for event in database.publication_events()
                if event["event"] == "verified" and event["plan_fingerprint"] == saved["plan_fingerprint"]]
    assert {event["day"] for event in verified} == {"Monday", "Thursday"}


@pytest.mark.parametrize("remote_changes", [False, True])
def test_return_to_historical_fingerprint_still_allows_approval_for_current_version(published, remote_changes):
    database, original, catalog, api = published
    payload = plan()
    if remote_changes:
        payload["days"][0]["exercises"][0]["weight_kg"] = 15
    else:
        payload["goal"] = "Changed goal with identical remote prescriptions"
    save(database, payload)
    approve(database, api, prepare_update_review(database, api, catalog, "kg"))
    restored = save(database, plan())
    assert restored["plan_fingerprint"] == original["plan_fingerprint"] and restored["plan_version"] == 3
    review = prepare_update_review(database, api, catalog, "kg")
    assert review["already_current"]  # Historical helper remains unchanged.
    assert review["can_approve"] and review["changes_count"] == int(remote_changes)
    assert approve(database, api, review)["updated_count"] == int(remote_changes)
    verified = [event for event in database.publication_events()
                if event["event"] == "verified" and event["version"] == 3]
    assert {event["day"] for event in verified} == {"Monday", "Thursday"}


@pytest.mark.parametrize("problem", ["new_day", "deleted_id", "new_plan_id", "missing_baseline", "shared_id"])
def test_missing_or_unsafe_bindings_block_without_any_write(published, problem):
    database, saved, catalog, api = published
    payload = plan()
    if problem == "new_day":
        payload["days"].append({**deepcopy(payload["days"][0]), "day": "Saturday"})
    elif problem == "deleted_id":
        api.routines.pop(next(iter(api.routines)))
    elif problem == "new_plan_id":
        payload["plan_id"] = "unbound-plan"
    else:
        verified = [event for event in database.publication_events() if event["event"] == "verified"]
        event = deepcopy(verified[-1])
        event["plan_fingerprint"] = "no-matching-baseline"
        if problem == "shared_id":
            event["routine_id"] = verified[0]["routine_id"]
            pending = next(item for item in database.publication_events()
                           if item["event"] == "create_pending" and item["day"] == event["day"])
            event["payload"] = pending["payload"]
        database.save_coach_reply("now", "hevy_publication", "Fixture binding", event)
    if problem in {"new_day", "new_plan_id"}:
        save(database, payload)
    before = deepcopy(database.publication_events())
    with pytest.raises(PublicationBlocked):
        prepare_update_review(database, api, catalog, "kg")
    assert api.writes == [] and database.publication_events() == before


@pytest.mark.parametrize("field", ["load", "notes", "rest", "folder", "order", "superset"])
def test_manual_edits_block_preview_even_if_they_match_the_new_desired_payload(published, field):
    database, _, catalog, api = published
    payload = plan()
    payload["days"][0]["exercises"][0]["weight_kg"] = 15
    save(database, payload)
    monday_id = next(event["routine_id"] for event in database.publication_events()
                     if event["event"] == "verified" and event["day"] == "Monday")
    current = api.routines[monday_id]
    if field == "load":
        for item in current["exercises"][0]["sets"]:
            item["weight_kg"] = 15
    elif field == "notes": current["exercises"][0]["notes"] = "My manual note"
    elif field == "rest": current["exercises"][0]["rest_seconds"] = 30
    elif field == "folder": current["folder_id"] = 999
    elif field == "order": current["exercises"].append(deepcopy(current["exercises"][0]))
    elif field == "superset": current["exercises"][0]["superset_id"] = 1
    with pytest.raises(PublicationBlocked, match="manually edited"):
        prepare_update_review(database, api, catalog, "kg")
    assert api.writes == []


def test_get_omits_unsupported_parent_notes_and_put_preserves_exercise_notes(published):
    database, _, catalog, api = published
    pending = next(event for event in database.publication_events() if event["event"] == "create_pending")
    assert pending["payload"]["notes"]  # Legacy initial-publication baseline.
    routine_id = next(iter(api.routines))
    assert "notes" not in api.request("GET", "/v1/routines/" + routine_id)["routine"]
    current = prepare_update_review(database, api, catalog, "kg")
    assert current["already_current"] and current["changes_count"] == 0
    _, review = changed_review(published)
    assert all("notes" not in entry["desired"] for entry in review["entries"])
    assert approve(database, api, review)["updated_count"] == 1
    assert "notes" not in api.put_payloads[0]
    assert api.put_payloads[0]["exercises"][0]["notes"] == "Conservative proposed trial load."
    assert prepare_update_review(database, api, catalog, "kg")["already_current"]


def test_review_cannot_introduce_arbitrary_parent_notes(published):
    database, _, _, api = published
    _, review = changed_review(published)
    review["entries"][0]["desired"]["notes"] = "An unsupported parent-level edit"
    before = deepcopy(database.publication_events())
    with pytest.raises(PublicationBlocked, match="Routine-level notes are unsupported"):
        approve(database, api, review)
    assert api.writes == [] and database.publication_events() == before


@pytest.mark.parametrize("problem", ["approval", "blank_note", "new_plan", "old_review", "new_version"])
def test_stale_or_unapproved_fingerprints_block_before_audit_or_writes(published, problem):
    database, _, _, api = published
    saved, review = changed_review(published)
    fingerprint, note = saved["plan_fingerprint"], "Human approved"
    if problem == "approval": fingerprint = "wrong"
    elif problem == "blank_note": note = "  "
    elif problem == "old_review": review["plan_fingerprint"] = "wrong"
    elif problem == "new_version": review["plan_version"] -= 1
    else:
        payload = plan()
        payload["goal"] = "Another plan saved while review was open"
        save(database, payload)
    before = deepcopy(database.publication_events())
    api.calls.clear()
    with pytest.raises(PublicationBlocked, match="explicit human approval"):
        apply_update_review(database, api, review, approved_fingerprint=fingerprint, approval_note=note)
    assert api.calls == [] and database.publication_events() == before


def test_last_snapshot_conflict_rejects_before_any_put_or_approval_audit(published):
    database, _, _, api = published
    _, review = changed_review(published, both=True)
    last_id = review["entries"][-1]["routine_id"]
    api.routines[last_id]["exercises"][0]["notes"] = "Manual edit after review"
    before = deepcopy(database.publication_events())
    api.calls.clear()
    with pytest.raises(PublicationBlocked):
        approve(database, api, review)
    assert len(api.calls) == 2 and api.writes == []
    assert database.publication_events() == before


def test_snapshot_digest_itself_must_match(published):
    database, _, _, api = published
    _, review = changed_review(published, both=True)
    review["entries"][-1]["current_snapshot"] = "stale"
    before = deepcopy(database.publication_events())
    with pytest.raises(PublicationBlocked, match="snapshot changed"):
        approve(database, api, review)
    assert api.writes == [] and database.publication_events() == before


def test_lost_put_response_reconciles_only_after_new_approval_without_repeating_write(published):
    database, _, catalog, api = published
    saved, review = changed_review(published, both=True)
    api.fail_put = "after"
    with pytest.raises(PublicationBlocked, match="No automatic write retry"):
        approve(database, api, review)
    assert len(api.writes) == 1 and api.writes[0][0] == "PUT"
    assert not publication_summary(database, saved)["complete"]
    events_before = deepcopy(database.publication_events())
    with pytest.raises(PublicationBlocked):
        approve(database, api, review)
    assert len(api.writes) == 1 and database.publication_events() == events_before
    fresh = prepare_update_review(database, api, catalog, "kg")
    assert database.publication_events() == events_before
    assert fresh["entries"][0]["reconciled"] and not fresh["entries"][0]["changed"]
    assert fresh["changes_count"] == 1 and fresh["can_approve"]
    result = approve(database, api, fresh)
    assert result["complete"] and result["updated_count"] == 1
    assert len(api.writes) == 2 and all(method == "PUT" for method, _ in api.writes)
    assert len({path for _, path in api.writes}) == 2
    assert len(api.routines) == 2


@pytest.mark.parametrize("failure", ["before", "mismatch"])
def test_unresolved_pending_is_never_retried(published, failure):
    database, _, catalog, api = published
    _, review = changed_review(published)
    api.fail_put = failure
    with pytest.raises(PublicationBlocked):
        approve(database, api, review)
    events_before = deepcopy(database.publication_events())
    with pytest.raises(PublicationBlocked, match="unresolved"):
        prepare_update_review(database, api, catalog, "kg")
    assert len(api.writes) == 1 and database.publication_events() == events_before
    assert events_before[-1]["event"] == "update_pending"


def test_pending_for_another_plan_cannot_reconcile_even_when_remote_matches_desired(published):
    database, _, catalog, api = published
    _, review = changed_review(published)
    api.fail_put = "after"
    with pytest.raises(PublicationBlocked): approve(database, api, review)
    payload = plan()
    payload["days"][0]["exercises"][0]["weight_kg"] = 15
    payload["goal"] = "Another version with identical mapped prescriptions"
    save(database, payload)
    with pytest.raises(PublicationBlocked, match="unresolved"):
        prepare_update_review(database, api, catalog, "kg")
    assert len(api.writes) == 1


def test_lost_response_with_no_remaining_changes_only_verifies_on_approval(published):
    database, _, catalog, api = published
    _, review = changed_review(published)
    api.fail_put = "after"
    with pytest.raises(PublicationBlocked):
        approve(database, api, review)
    before = deepcopy(database.publication_events())
    fresh = prepare_update_review(database, api, catalog, "kg")
    assert fresh["changes_count"] == 0 and fresh["can_approve"] and not fresh["already_current"]
    assert database.publication_events() == before
    assert approve(database, api, fresh)["updated_count"] == 0
    assert len(api.writes) == 1
    assert prepare_update_review(database, api, catalog, "kg")["already_current"]


@pytest.mark.parametrize("point", ["preflight", "approval_audit"])
def test_plan_saved_during_preflight_or_approval_stops_before_put(published, monkeypatch, point):
    database, _, _, api = published
    _, review = changed_review(published)
    payload = plan()
    payload["goal"] = "New plan saved during update approval"
    events_before = deepcopy(database.publication_events())
    if point == "preflight":
        original = api.request
        def request(method, path, **kwargs):
            result = original(method, path, **kwargs)
            save(database, payload)
            return result
        monkeypatch.setattr(api, "request", request)
    else:
        original = database.save_coach_reply
        def save_reply(created_at, status, question, body):
            original(created_at, status, question, body)
            if body.get("event") == "update_approved":
                save(database, payload)
        monkeypatch.setattr(database, "save_coach_reply", save_reply)
    with pytest.raises(PublicationBlocked, match="latest saved plan"):
        approve(database, api, review)
    assert api.writes == []
    assert not any(event["event"] == "update_pending" for event in database.publication_events())
    if point == "preflight":
        assert database.publication_events() == events_before


@pytest.mark.parametrize("problem", ["unknown", "ambiguous", "type"])
def test_catalog_mapping_rejected_before_any_remote_call(published, problem):
    database, _, catalog, api = published
    if problem == "unknown": catalog.clear()
    elif problem == "ambiguous": catalog.append(deepcopy(catalog[0]))
    else: catalog[0]["type"] = "reps_only"
    before = deepcopy(database.publication_events())
    with pytest.raises(PublicationBlocked):
        prepare_update_review(database, api, catalog, "kg")
    assert api.calls == [] and database.publication_events() == before


def test_pending_audit_failure_prevents_put(published, monkeypatch):
    database, _, _, api = published
    _, review = changed_review(published)
    original = database.save_coach_reply
    def save_reply(created_at, status, question, body):
        if body.get("event") == "update_pending":
            raise RuntimeError("Pending audit persistence unavailable")
        original(created_at, status, question, body)
    monkeypatch.setattr(database, "save_coach_reply", save_reply)
    with pytest.raises(RuntimeError, match="Pending audit"):
        approve(database, api, review)
    assert api.writes == [] and database.publication_events()[-1]["event"] == "update_approved"


def test_verified_payload_is_baseline_for_subsequent_updates(published):
    database, _, catalog, api = published
    _, review = changed_review(published)
    approve(database, api, review)
    payload = plan()
    payload["days"][0]["exercises"][0]["weight_kg"] = 20
    save(database, payload)
    fresh = prepare_update_review(database, api, catalog, "kg")
    assert fresh["entries"][0]["baseline"]["exercises"][0]["sets"][0]["weight_kg"] == 15
    assert approve(database, api, fresh)["updated_count"] == 1
    assert len(api.writes) == 2


def test_approval_audit_failure_prevents_remote_writes(published, monkeypatch):
    database, _, _, api = published
    _, review = changed_review(published)
    def fail(*args, **kwargs): raise RuntimeError("Audit persistence unavailable")
    monkeypatch.setattr(database, "save_coach_reply", fail)
    with pytest.raises(RuntimeError, match="Audit persistence"):
        approve(database, api, review)
    assert api.writes == []


def test_shared_publication_lock_blocks_update(published, monkeypatch, tmp_path):
    database, _, _, api = published
    _, review = changed_review(published)
    path = tmp_path / "shared" / "fitness.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    path.parent.mkdir()
    with (path.parent / "hevy-publication.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PublicationBlocked, match="already in progress"):
            approve(database, api, review)
        fcntl.flock(lock, fcntl.LOCK_UN)
    assert api.writes == []


def test_update_audits_survive_chat_retention_and_stay_out_of_chat(published):
    database, _, _, api = published
    saved, review = changed_review(published)
    approve(database, api, review)
    before = deepcopy(database.publication_events())
    for index in range(30):
        database.save_coach_reply(str(index), "answered", "question", {"message": "answer"})
    assert database.publication_events() == before
    assert database.latest_coach_reply()["status"] == "answered"
    assert database.latest_training_plan()["plan_version"] == 2
    assert publication_summary(database, saved)["complete"]
    with database.transaction() as connection:
        records = connection.execute("SELECT status, body_json FROM coach_replies").fetchall()
    assert sum(row["status"] == "plan_draft" for row in records) == 2
    assert all(row["status"] == "hevy_publication" for row in records
               if json.loads(row["body_json"]).get("event") in {"update_approved", "update_pending", "verified"})
