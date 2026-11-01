"""Macro guide calculated from stored numbers. Missing inputs stay missing."""

from __future__ import annotations

TARGET_LB = 170
MODEST_DEFICIT = 300
PROTEIN_G_PER_KG = 2.0
FAT_G_PER_KG = 0.8

ACTIVITY_FACTORS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "high": 1.725,
}

PRIMARY_GOAL = "Drop to 170 lb while getting stronger progressively."


def macro_guide(
    *,
    weight_kg: float | None,
    height_cm: float | None,
    age_years: int | None,
    sex: str | None,
    activity: str | None,
) -> dict:
    """Modest cut that still leaves room to get stronger. No guessed body size."""
    missing: list[str] = []
    if weight_kg is None or weight_kg <= 0:
        missing.append("current weight")
    if height_cm is None or height_cm <= 0:
        missing.append("height")
    if age_years is None or age_years <= 0:
        missing.append("age")
    if sex not in {"female", "male"}:
        missing.append("sex")
    if activity not in ACTIVITY_FACTORS:
        missing.append("activity")
    base = {
        "target_lb": TARGET_LB,
        "complete": not missing,
        "missing": missing,
        "calories": None,
        "protein_g": None,
        "fat_g": None,
        "carb_g": None,
        "note": (
            "This is a food guide for losing weight while getting stronger. "
            "It is not a medical plan."
        ),
    }
    if missing:
        return base
    assert weight_kg is not None and height_cm is not None and age_years is not None
    bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age_years)
    bmr += 5 if sex == "male" else -161
    maintenance = bmr * ACTIVITY_FACTORS[str(activity)]
    calories = int(round(maintenance - MODEST_DEFICIT))
    protein_g = int(round(weight_kg * PROTEIN_G_PER_KG))
    fat_g = int(round(weight_kg * FAT_G_PER_KG))
    remaining = calories - (protein_g * 4) - (fat_g * 9)
    if remaining < 0:
        base["missing"] = ["room for carbohydrates at this deficit"]
        base["complete"] = False
        return base
    base["calories"] = calories
    base["protein_g"] = protein_g
    base["fat_g"] = fat_g
    base["carb_g"] = int(round(remaining / 4))
    return base


def ensure_primary_goal(database, created_at: str) -> None:
    for goal in database.list_goals():
        text = goal["statement"].lower()
        if "170" in text and "stronger" in text:
            return
    database.save_goal(PRIMARY_GOAL, created_at)
