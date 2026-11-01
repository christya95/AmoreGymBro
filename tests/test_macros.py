from app.macros import ensure_primary_goal, macro_guide
from tests.test_app import signed_in


def test_missing_body_inputs_do_not_invent_macros():
    guide = macro_guide(
        weight_kg=80,
        height_cm=None,
        age_years=None,
        sex=None,
        activity=None,
    )
    assert guide["complete"] is False
    assert guide["calories"] is None
    assert guide["protein_g"] is None
    assert "height" in guide["missing"]
    assert guide["target_lb"] == 170


def test_goal_page_saves_170_and_hides_macros_until_inputs_exist(tmp_path):
    client, app = signed_in(tmp_path)
    page = client.get("/goals")
    assert "Drop to 170 lb while getting stronger progressively." in page.text
    assert "Still needed" in page.text
    assert "<dt>Calories</dt>" not in page.text
    again = client.get("/goals")
    statements = [item["statement"] for item in app.state.db.list_goals()]
    assert statements.count("Drop to 170 lb while getting stronger progressively.") == 1
    assert "170" in again.text


def test_hevy_measurement_does_not_invent_height(tmp_path):
    _client, app = signed_in(tmp_path)
    status = app.state.db.upsert_body_measurement(
        {"date": "2026-10-01", "weight_kg": 82.5, "fat_percent": 18},
        "2026-10-31T22:00:00Z",
    )
    assert status == "upserted"
    stored = app.state.db.latest_body_measurement()
    assert stored["weight_kg"] == 82.5
    assert "height" not in stored or stored.get("height_cm") is None
    guide = macro_guide(
        weight_kg=stored["weight_kg"],
        height_cm=None,
        age_years=None,
        sex=None,
        activity=None,
    )
    assert guide["calories"] is None


def test_user_height_is_their_own_entry(tmp_path):
    client, app = signed_in(tmp_path)
    saved = client.post(
        "/goals/body",
        data={"height_in": "70", "age_years": "35", "sex": "male", "activity": "moderate"},
        follow_redirects=True,
    )
    assert "Body details saved." in saved.text
    assert "Your report" in saved.text
    entry = app.state.db.get_body_entry()
    assert entry["height_cm"] == 177.8
    assert entry["sex"] == "male"
    ensure_primary_goal(app.state.db, "2026-10-31T22:00:00Z")
    assert len(app.state.db.list_goals()) == 1
