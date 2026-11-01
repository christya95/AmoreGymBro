from datetime import datetime, timedelta, timezone
from copy import deepcopy
import json
import re

import pytest

from app.hevy.routine_publish import publish_initial
from app.training_plan import WeeklyPlan, save_plan_proposal
from tests.test_app import signed_in
from tests.test_training_plan import example_plan
from tests.test_routine_update import FakeAPI

NOW = '2026-11-01T12:00:00Z'


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'coach.db'))
    client, app = signed_in(tmp_path)
    plan = WeeklyPlan.model_validate(example_plan())
    saved = save_plan_proposal(app.state.db, plan, NOW)
    catalog = [{'title': 'Machine press', 'id': 'press', 'type': 'weight_reps'}]
    (tmp_path / 'hevy-exercise-catalog.json').write_text(json.dumps(catalog))
    api = FakeAPI(app.state.db)
    api.close = lambda: None
    publish_initial(app.state.db, api, catalog, approved_fingerprint=saved['plan_fingerprint'], approval_note='Explicit test approval')
    api.calls.clear()
    app.state.routine_api_factory = lambda: api
    return client, app, api


def changed(app):
    plan = deepcopy(app.state.db.latest_training_plan()['weekly_plan'])
    plan['days'][0]['exercises'][0]['weight_kg'] = 12.5
    return save_plan_proposal(app.state.db, WeeklyPlan.model_validate(plan), NOW)


def fields(page):
    return {name: re.search('name="' + name + '" value="([^"]+)"', page.text)[1]
            for name in ('csrf_token', 'approval_token')}


def test_save_review_is_read_only_then_approval_updates_same_id(tmp_path, monkeypatch):
    client, app, api = setup(tmp_path, monkeypatch)
    original_ids = set(api.routines)
    page = client.get('/plan/hevy')
    assert 'Already up to date' in page.text and not api.writes
    changed(app)
    page = client.get('/plan/hevy')
    assert page.status_code == 200 and '12.5 kg' in page.text and '10 kg' in page.text
    assert 'Approve &amp; save to Hevy' in page.text and not api.writes
    data = fields(page)
    assert client.post('/plan/hevy/approve', data={'approval_token': data['approval_token']}).status_code == 403
    response = client.post('/plan/hevy/approve', data=data)
    assert response.status_code == 200 and 'Saved and verified in Hevy' in response.text
    assert set(api.routines) == original_ids
    assert [method for method, _ in api.writes] == ['PUT']
    assert client.post('/plan/hevy/approve', data=data).status_code == 409
    assert len(api.writes) == 1


@pytest.mark.parametrize('case', ['expired', 'stale_plan', 'manual_edit', 'wrong_session'])
def test_stale_or_invalid_review_cannot_write(tmp_path, monkeypatch, case):
    client, app, api = setup(tmp_path, monkeypatch)
    changed(app)
    data = fields(client.get('/plan/hevy'))
    if case == 'expired':
        app.state.hevy_reviews[data['approval_token']]['expires'] = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif case == 'stale_plan':
        plan = deepcopy(app.state.db.latest_training_plan()['weekly_plan']); plan['phase'] = 'Changed after review'
        save_plan_proposal(app.state.db, WeeklyPlan.model_validate(plan), NOW)
    elif case == 'manual_edit':
        next(iter(api.routines.values()))['exercises'][0]['notes'] = 'Manual edit after review'
    else:
        from fastapi.testclient import TestClient
        from tests.test_app import PASSWORD
        another = TestClient(app); another.post('/login', data={'password': PASSWORD})
        # Even possession of another signed-in user's token is insufficient.
        assert another.post('/plan/hevy/approve', data=data).status_code == 403
        assert not api.writes
        return
    response = client.post('/plan/hevy/approve', data=data)
    assert response.status_code in (200, 409)
    assert not api.writes


def test_missing_catalog_binding_and_anonymous_review_are_safe(tmp_path, monkeypatch):
    client, app, api = setup(tmp_path, monkeypatch)
    (tmp_path / 'hevy-exercise-catalog.json').unlink()
    assert 'Save paused' in client.get('/plan/hevy').text
    assert not api.writes
    client.cookies.clear()
    assert client.get('/plan/hevy').url.path == '/login'
    assert client.post('/plan/hevy/approve', follow_redirects=False).status_code == 303


def test_restored_fingerprint_still_offers_approval_for_actual_changes(tmp_path, monkeypatch):
    client, app, api = setup(tmp_path, monkeypatch)
    original = deepcopy(app.state.db.latest_training_plan()['weekly_plan'])
    changed(app)
    data = fields(client.get('/plan/hevy'))
    client.post('/plan/hevy/approve', data=data)
    save_plan_proposal(app.state.db, WeeklyPlan.model_validate(original), NOW)
    api.calls.clear()
    review = client.get('/plan/hevy')
    assert 'Approve &amp; save to Hevy' in review.text
    assert 'Already up to date' not in review.text
    assert not api.writes
