"""Versioned local plan proposals. This module never calls Hevy or a model."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class ExercisePrescription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    sets: int = Field(ge=1, le=5)
    reps_min: int | None = Field(default=None, ge=1, le=30)
    reps_max: int | None = Field(default=None, ge=1, le=30)
    duration_minutes: int | None = Field(default=None, ge=1, le=60)
    weight_kg: float | None = Field(default=None, ge=0, le=300, allow_inf_nan=False)
    load_basis: Literal["machine_display", "per_dumbbell", "per_arm", "bodyweight", "cardio"]
    rest_seconds: int = Field(default=90, ge=0, le=300)
    note: str = Field(max_length=500)
    reference: str = Field(default="", max_length=250)

    @model_validator(mode="after")
    def coherent(self):
        if self.duration_minutes is None and (self.reps_min is None or self.reps_max is None):
            raise ValueError("A prescription needs reps or a duration")
        if self.reps_min is not None and self.reps_max is not None and self.reps_min > self.reps_max:
            raise ValueError("Repetition range is reversed")
        if self.load_basis in {"machine_display", "per_dumbbell", "per_arm"} and not self.weight_kg:
            raise ValueError("Loaded prescriptions need an explicit trial load")
        if self.load_basis in {"bodyweight", "cardio"} and self.weight_kg is not None:
            raise ValueError("Bodyweight/cardio are not missing or zero-weight lifts")
        return self


class TrainingDay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    name: str = Field(min_length=1, max_length=100)
    focus: str = Field(max_length=120)
    exercises: list[ExercisePrescription] = Field(min_length=1, max_length=8)


class PlanSource(BaseModel):
    title: str = Field(max_length=150)
    url: HttpUrl


class WeeklyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(pattern=r"^[a-z0-9-]{1,60}$")
    name: str = Field(min_length=1, max_length=100)
    goal: str = Field(max_length=400)
    phase: str = Field(max_length=250)
    intake: list[str] = Field(min_length=1, max_length=8)
    rules: list[str] = Field(min_length=1, max_length=12)
    sources: list[PlanSource] = Field(min_length=1, max_length=4)
    days: list[TrainingDay] = Field(min_length=1, max_length=7)

    @model_validator(mode="after")
    def unique_days(self):
        if len({day.day for day in self.days}) != len(self.days):
            raise ValueError("Duplicate training day")
        if any(len(item) > 650 for item in self.intake + self.rules):
            raise ValueError("Plan notes are too long")
        if any(source.url.scheme != "https" for source in self.sources):
            raise ValueError("Research links must use HTTPS")
        return self


def save_plan_proposal(database, plan: WeeklyPlan, now: str) -> dict:
    payload = plan.model_dump(mode="json")
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    previous = database.latest_training_plan()
    if previous and previous.get("plan_fingerprint") == fingerprint:
        return previous
    version = (previous.get("plan_version", 0) if previous else 0) + 1
    body = {
        "message": "Weekly plan draft. Proposed training, not completed workouts. Nothing has been written to Hevy.",
        "suggested_action": "Big Dawg, we’re rebuilding the engine before entering the drag race. Review the daily plan and calibrate the trial loads.",
        "weekly_plan": payload, "plan_version": version, "plan_fingerprint": fingerprint,
        "observed_facts": [], "missing_information": [], "supporting_workout_ids": [],
        "uncertainty": "Trial loads are proposals based on a small saved history, not measured current capacity. I cannot observe exercise form or diagnose injuries.",
    }
    database.save_coach_reply(now, "plan_draft", "Create my weekly training plan", body)
    return database.latest_training_plan()
