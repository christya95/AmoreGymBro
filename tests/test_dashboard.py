from app.dashboard import dashboard_data, weight_chart
from tests.test_app import signed_in


def test_single_weight_is_one_dot_without_invented_change():
    chart = weight_chart([{"date": "2026-10-21", "value": 185.3}])
    assert len(chart["points"]) == 1
    assert chart["points"][0]["x"] == 160
    assert chart["path"] == ""
    assert chart["change"] is None


def test_weight_chart_uses_elapsed_calendar_days_and_keeps_direction():
    chart = weight_chart([
        {"date": "2026-10-11", "value": 182},
        {"date": "2026-10-01", "value": 185},
        {"date": "2026-10-02", "value": 184},
    ])
    points = chart["points"]
    assert [p["x"] for p in points] == [12, 41.6, 308]
    assert points[0]["y"] < points[-1]["y"]
    assert chart["change"] == -3


def test_dashboard_missing_data_is_not_filled_with_fake_values(tmp_path):
    client, app = signed_in(tmp_path)
    data = dashboard_data(app.state.db, "lbs")
    assert data["weight_line"] == "—"
    assert data["chart"]["points"] == []
    assert data["sessions"] == []
    page = client.get("/")
    assert page.status_code == 200
    assert 'aria-current="page"' in page.text
    assert 'Your weight trend starts' in page.text


def test_dashboard_weight_is_latest_nonmissing_and_converted_once(tmp_path):
    _, app = signed_in(tmp_path)
    db = app.state.db
    for day, weight in [("2026-10-01", 90), ("2026-10-21", 84), ("2026-10-25", None)]:
        db.upsert_body_measurement({"date": day, "weight_kg": weight}, "2026-10-31T18:00:00Z")
    data = dashboard_data(db, "lbs")
    assert data["weight_line"] == "185.2 lb"
    assert data["weight_date"] == "2026-10-21"
    assert len(data["chart"]["points"]) == 2
    assert data["chart"]["change"] == -13.2
    assert db.body_weight_history(limit=1)[0]["weight_kg"] == 84


def test_session_volume_excludes_warmups_and_deleted_records(tmp_path):
    _, app = signed_in(tmp_path)
    db = app.state.db
    for workout_id in ["active", "deleted"]:
        db.apply_workout({
            "id": workout_id, "title": workout_id,
            "start_time": "2026-10-20T12:00:00Z", "updated_at": "2026-10-20T12:00:00Z",
            "exercises": [{"index": 0, "title": "Squat", "exercise_template_id": "squat", "sets": [
                {"index": 0, "type": "normal", "weight_kg": 50, "reps": 5},
                {"index": 1, "type": "warmup", "weight_kg": 20, "reps": 10},
                {"index": 2, "type": "normal", "weight_kg": None, "reps": 5},
            ]}],
        }, "2026-10-31T12:00:00Z")
    db.mark_deleted("deleted", "2026-10-21T12:00:00Z", "2026-10-31T12:00:00Z")
    data = dashboard_data(db, "kg")
    assert data["totals"]["confirmed_workout_count"] == 1
    assert data["totals"]["working_set_count"] == 2
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["volume"] == 250
    assert data["sessions"][0]["height"] == 100
    assert data["missing_load"] == 1


def test_home_schedule_uses_saved_plan_and_local_weekday_without_marking_completion(tmp_path):
    from datetime import datetime, timezone
    from app.dashboard import home_brief
    from app.training_plan import WeeklyPlan, save_plan_proposal
    from tests.test_training_plan import example_plan
    _, app = signed_in(tmp_path)
    plan = example_plan()
    save_plan_proposal(app.state.db, WeeklyPlan.model_validate(plan), '2026-11-01T00:00:00Z')
    # It is already Monday in UTC, while Toronto is still Sunday.
    instant = datetime(2026,11,2,1,0,tzinfo=timezone.utc)
    local = home_brief(app.state.db, 'America/Toronto', now=instant)
    assert local['weekday']=='Sunday' and local['today'] is None
    assert local['has_plan'] and local['next_session']['day']=='Monday'
    assert local['next_session']['date']=='2026-11-02'
    utc = home_brief(app.state.db, 'UTC', now=instant)
    assert utc['today']['name']=='A comedic upper day'
    assert utc['today']['exercise_count']==1 and utc['today']['lifting_sets']==2
    assert utc['today']['cardio_minutes']==0
    assert 'completed' not in utc['today']
    assert app.state.db.count_active()==0


def test_home_plan_separates_cardio_minutes_from_lifting_sets(tmp_path):
    from datetime import datetime, timezone
    from app.dashboard import home_brief
    from app.training_plan import WeeklyPlan, save_plan_proposal
    from tests.test_training_plan import example_plan
    _,app=signed_in(tmp_path)
    plan=example_plan()
    plan['days'][0]['exercises'].append({'name':'Treadmill','sets':1,'duration_minutes':15,'weight_kg':None,'load_basis':'cardio','rest_seconds':0,'note':'Proposed easy pace.'})
    save_plan_proposal(app.state.db, WeeklyPlan.model_validate(plan), '2026-11-01T00:00:00Z')
    brief=home_brief(app.state.db,'UTC',now=datetime(2026,11,2,12,tzinfo=timezone.utc))
    assert brief['today']['exercise_count']==2
    assert brief['today']['lifting_sets']==2 and brief['today']['cardio_minutes']==15


def test_home_without_plan_does_not_guess_a_workout(tmp_path):
    from app.dashboard import home_brief
    _,app=signed_in(tmp_path)
    brief=home_brief(app.state.db)
    assert not brief['has_plan'] and brief['today'] is None and brief['next_session'] is None


def test_home_keeps_global_navigation_single_and_garmin_actions_visible(tmp_path):
    from tests.test_training_plan import example_plan
    from app.training_plan import WeeklyPlan,save_plan_proposal
    from tests.test_ui import Page
    client,app=signed_in(tmp_path,garmin_api_key='synthetic-private-key')
    save_plan_proposal(app.state.db,WeeklyPlan.model_validate(example_plan()),'2026-11-01T00:00:00Z')
    page=Page(client.get('/').text)
    for href in ['/coach','/check-ins','/history']:
        assert sum(tag=='a' and attrs.get('href')==href for tag,attrs in page.elements)==1
    assert any(tag=='a' and attrs.get('href')=='/plan' for tag,attrs in page.elements)
    assert page.panels_for(action='/garmin/sync')==()
    assert page.panels_for(href='/garmin')==()
