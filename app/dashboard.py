"""Dashboard presentation calculated from confirmed local records, without AI calls."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from math import isfinite

from app.metrics import summarize
from app.present import KG_TO_LB, format_weight


def dashboard_data(database, unit: str | None) -> dict:
    summary = summarize(database.confirmed_exercise_rows(), database.count_active())
    volumes: dict[str, dict] = {}
    for exercise in summary["exercises"]:
        for session in exercise["sessions"]:
            workout_id = session["workout_id"]
            item = volumes.get(workout_id)
            if item is None:
                item = {
                    "id": workout_id, "title": session["workout_title"],
                    "date": session["start_time"], "volume": None,
                }
                volumes[workout_id] = item
            if session["session_volume_kg"] is not None:
                item["volume"] = (item["volume"] or 0) + session["session_volume_kg"]
    sessions = sorted(volumes.values(), key=lambda s: (s["date"] or "", s["id"]))[-7:]
    maximum = max((s["volume"] or 0 for s in sessions), default=0)
    for session in sessions:
        if maximum and session["volume"] is not None:
            session["height"] = round(100 * session["volume"] / maximum, 2)
        else:
            session["height"] = 0
        session["volume_line"] = format_weight(session["volume"], unit)
        session["label"] = _date_label(session["date"])
    weights = database.body_weight_history(limit=30)
    factor = KG_TO_LB if unit == "lbs" else 1
    points = [{"date": row["date"], "value": round(row["weight_kg"] * factor, 2)} for row in weights]
    chart = weight_chart(points)
    return {
        "totals": summary["totals"], "exercise_count": len(summary["exercises"]),
        "weight_line": format_weight(weights[-1]["weight_kg"] if weights else None, unit),
        "weight_date": weights[-1]["date"] if weights else None,
        "weight_unit": "lb" if unit == "lbs" else "kg", "chart": chart,
        "sessions": sessions, "goal": next(iter(database.list_goals()), None),
        "check_in": next(iter(database.list_check_ins(limit=1)), None),
        "missing_load": summary["totals"]["sets_missing_load"],
    }


def weight_chart(points: list[dict]) -> dict:
    """Use calendar spacing. One observation gets one dot, never an invented trend."""
    valid = []
    for point in points:
        try:
            day = date.fromisoformat(point["date"])
            value = float(point["value"])
        except (ValueError, TypeError, KeyError):
            continue
        if isfinite(value) and value > 0:
            valid.append({**point, "day": day, "value": value})
    valid.sort(key=lambda p: p["day"])
    if not valid:
        return {"points": [], "path": "", "area": "", "change": None}
    low, high = min(p["value"] for p in valid), max(p["value"] for p in valid)
    span = max(high - low, 2)
    center = (low + high) / 2
    first_day = valid[0]["day"]
    elapsed_days = (valid[-1]["day"] - first_day).days
    for point in valid:
        if elapsed_days:
            point["x"] = round(12 + 296 * (point["day"] - first_day).days / elapsed_days, 2)
        else:
            point["x"] = 160
        point["y"] = round(75 - (point["value"] - center) / span * 90, 2)
        point["label"] = _date_label(point["date"])
    path = ""
    if len(valid) > 1:
        path = " ".join(f"{'M' if i == 0 else 'L'}{p['x']},{p['y']}" for i, p in enumerate(valid))
    return {
        "points": valid, "path": path,
        "area": f"{path} L{valid[-1]['x']},150 L{valid[0]['x']},150 Z" if path else "",
        "change": round(valid[-1]["value"] - valid[0]["value"], 1) if len(valid) > 1 else None,
        "first_label": valid[0]["label"], "last_label": valid[-1]["label"],
        "high": round(center + span / 2, 1), "low": round(center - span / 2, 1),
    }


def _date_label(value: str | None) -> str:
    try:
        return date.fromisoformat((value or "")[:10]).strftime("%b %d")
    except ValueError:
        return "Undated"


def home_brief(database, timezone_name="America/Toronto", *, now=None):
    """Saved-plan schedule for the local calendar day; never a completion/readiness claim."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(timezone_name)).date()
    weekday = today.strftime("%A")
    saved = database.latest_training_plan()
    plan = saved.get("weekly_plan") if saved else None
    days = {item["day"]: item for item in plan["days"]} if plan else {}
    day = days.get(weekday)
    scheduled = None
    if day:
        scheduled = {"name": day["name"], "focus": day["focus"],
            "exercise_count": len(day["exercises"]),
            "lifting_sets": sum(
                exercise["sets"] for exercise in day["exercises"]
                if exercise["load_basis"] != "cardio"
            ),
            "cardio_minutes": sum(
                (exercise.get("duration_minutes") or 0) * exercise["sets"]
                for exercise in day["exercises"] if exercise["load_basis"] == "cardio"
            ),
        }
    next_session = None
    for offset in range(1, 8):
        future = today + timedelta(days=offset)
        future_weekday = future.strftime("%A")
        entry = days.get(future_weekday)
        if entry:
            next_session = {"day": future_weekday, "name": entry["name"], "date": future.isoformat()}
            break
    return {"date": today.isoformat(), "date_label": f"{today:%A}, {today:%b} {today.day}",
            "weekday": weekday, "has_plan": bool(plan),
            "plan_name": plan["name"] if plan else None, "today": scheduled, "next_session": next_session}
