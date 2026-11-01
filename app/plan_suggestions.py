"""Small chat proposals with explicit local approval; no model or Hevy writes."""
from __future__ import annotations

import json
import secrets
from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.present import format_weight
from app.training_plan import WeeklyPlan, save_plan_proposal


class SuggestionBlocked(ValueError):
    pass


class PlanChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    exercise: str = Field(min_length=1, max_length=100)
    replacement: str | None = Field(default=None, min_length=1, max_length=100)
    sets: int | None = Field(default=None, ge=1, le=5)
    reps_min: int | None = Field(default=None, ge=1, le=30)
    reps_max: int | None = Field(default=None, ge=1, le=30)
    weight_kg: float | None = Field(default=None, gt=0, le=300, allow_inf_nan=False)
    duration_minutes: int | None = Field(default=None, ge=1, le=60)
    load_basis: Literal["machine_display", "per_dumbbell", "per_arm", "bodyweight", "cardio"] | None = None
    reason: str = Field(min_length=1, max_length=160, description="Brief qualitative reason, not an invented performance claim.")


def load_catalog(settings):
    try:
        value = json.loads((Path(settings.database_path).parent / "hevy-exercise-catalog.json").read_text())
        if isinstance(value, dict):
            value = value.get("exercise_templates", [])
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def _description(item, unit):
    target = f'{item["sets"]} {"set" if item["sets"] == 1 else "sets"} × '
    if item.get("duration_minutes"):
        target += f'{item["duration_minutes"]} min'
    else:
        target += f'{item["reps_min"]}–{item["reps_max"]} reps'
    if item.get("weight_kg") is not None:
        basis = {"per_dumbbell": " each dumbbell", "per_arm": " per arm", "machine_display": " machine setting"}.get(item["load_basis"], "")
        target += ' · ' + format_weight(item["weight_kg"], unit) + basis
    else:
        target += ' · ' + item["load_basis"]
    return item["name"] + ' · ' + target


def prepare_suggestion(database, baseline, changes, catalog):
    """Validate bounded patches and build the exact candidate for human review."""
    if not baseline or not 1 <= len(changes) <= 3:
        raise SuggestionBlocked("A current plan and a small set of changes are needed.")
    current = database.latest_training_plan()
    if not current or current["plan_fingerprint"] != baseline["plan_fingerprint"]:
        raise SuggestionBlocked("Your plan changed while Zeus answered. Ask again using the current plan.")
    candidate = deepcopy(baseline["weekly_plan"])
    unit = (database.get_profile() or {}).get("weight_unit")
    seen, rows = set(), []
    for raw in changes:
        patch = PlanChange.model_validate(raw)
        key = (patch.day, patch.exercise)
        if key in seen:
            raise SuggestionBlocked("An exercise was changed twice in one suggestion.")
        seen.add(key)
        days = [d for d in candidate["days"] if d["day"] == patch.day]
        matches = [e for d in days for e in d["exercises"] if e["name"] == patch.exercise]
        if len(matches) != 1:
            raise SuggestionBlocked("Zeus must name an exercise already in your saved plan.")
        item = matches[0]
        original_basis = item["load_basis"]
        if patch.load_basis and patch.load_basis != original_basis and patch.load_basis in {"machine_display", "per_dumbbell", "per_arm"} and patch.weight_kg is None:
            raise SuggestionBlocked("A changed load basis needs an explicit trial load.")
        before = _description(item, unit)
        updates = patch.model_dump(exclude_none=True, exclude={"day", "exercise", "reason", "replacement"})
        if patch.replacement and patch.replacement != item["name"]:
            if not patch.load_basis:
                raise SuggestionBlocked("A swap needs an explicit load basis and trial prescription.")
            if patch.load_basis in {"machine_display", "per_dumbbell", "per_arm"} and patch.weight_kg is None:
                raise SuggestionBlocked("A loaded swap needs an explicit trial load.")
            item["name"] = patch.replacement
        if patch.load_basis in {"bodyweight", "cardio"}:
            item["weight_kg"] = None
        if patch.load_basis == "cardio":
            if patch.duration_minutes is None:
                raise SuggestionBlocked("Cardio needs an explicit duration.")
            item["reps_min"] = item["reps_max"] = None
        elif patch.load_basis in {"machine_display", "per_dumbbell", "per_arm", "bodyweight"}:
            item["duration_minutes"] = None
        item.update(updates)
        if item["load_basis"] != "cardio" and item.get("duration_minutes") is not None:
            raise SuggestionBlocked("A lifting prescription uses reps, not a cardio duration.")
        if item["load_basis"] == "cardio" and (item.get("reps_min") is not None or item.get("reps_max") is not None):
            raise SuggestionBlocked("A cardio prescription needs duration rather than reps.")
        after = _description(item, unit)
        if before == after:
            continue
        # Do not carry stale load-specific notes into a revised prescription.
        item["note"] = "Proposed trial target. Warm up, keep reps controlled and stop for pain. Adjust down if needed."
        item["reference"] = "Zeus chat suggestion approved separately; not a confirmed lift."
        rows.append({"day": patch.day, "before": before, "after": after, "reason": patch.reason})
    if not rows:
        raise SuggestionBlocked("There are no changes to apply.")
    try:
        plan = WeeklyPlan.model_validate(candidate)
        # Exact documented template/type validation, also for retained exercises.
        from app.hevy.routine_publish import prepare_payloads
        prepare_payloads(plan.model_dump(mode="json"), catalog)
    except Exception:
        raise SuggestionBlocked("The proposed exercise or prescription could not be verified. Ask Zeus for a supported alternative.") from None
    return {"suggestion_id": secrets.token_urlsafe(18), "event": "proposed",
            "baseline_fingerprint": baseline["plan_fingerprint"],
            "candidate": plan.model_dump(mode="json"), "changes": rows}


def record_suggestion(database, proposal, now):
    database.save_coach_reply(now, "plan_suggestion", "Plan suggestion audit", {**proposal, "created_at": now})


def suggestion_state(database, suggestion_id):
    events = [e for e in database.plan_suggestion_events() if e.get("suggestion_id") == suggestion_id]
    proposed = next((e for e in events if e.get("event") == "proposed"), None)
    if not proposed:
        raise SuggestionBlocked("This suggestion is no longer available.")
    state = events[-1]["event"]
    current = database.latest_training_plan()
    if state == "proposed" and (not current or current["plan_fingerprint"] != proposed["baseline_fingerprint"]):
        state = "stale"
    return proposed, state


def decide_suggestion(database, suggestion_id, decision, now):
    proposal, state = suggestion_state(database, suggestion_id)
    if state != "proposed":
        raise SuggestionBlocked("This suggestion was already handled or your plan has changed. Ask Zeus again.")
    if decision not in {"apply", "dismiss"}:
        raise SuggestionBlocked("Choose whether to update or keep your plan.")
    version = None
    if decision == "apply":
        saved = save_plan_proposal(database, WeeklyPlan.model_validate(proposal["candidate"]), now)
        version = saved["plan_version"]
    record_suggestion(database, {"suggestion_id": suggestion_id, "event": "applied" if decision == "apply" else "dismissed", "plan_version": version}, now)
    return version
