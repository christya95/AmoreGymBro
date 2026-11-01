from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.coach import _tools
from app.main import create_app
from app.training_plan import WeeklyPlan, save_plan_proposal
from tests.test_app import settings, signed_in


def example_plan():
    return {"plan_id": "test-plan", "name": "Test weekly plan", "goal": "Rebuild strength",
            "phase": "A proposed return phase", "intake": ["User report: returning to lifting"],
            "rules": ["Trial loads are proposed, not confirmed lifts."],
            "sources": [{"title": "Training guidance", "url": "https://pubmed.ncbi.nlm.nih.gov/41843416/"}],
            "days": [{"day": "Monday", "name": "A comedic upper day", "focus": "Upper body",
                      "exercises": [{"name": "Machine press", "sets": 2, "reps_min": 8, "reps_max": 12,
                                     "weight_kg": 10, "load_basis": "machine_display", "rest_seconds": 120,
                                     "note": "A conservative trial on the same machine.", "reference": "Earlier saved set"}]}]}


def test_plan_requires_login_and_does_not_make_paid_or_hevy_calls(tmp_path):
    app = create_app(settings(tmp_path))
    client = TestClient(app)
    assert client.get('/plan').url.path == '/login'
    response = client.post('/plan/draft', json=example_plan(), follow_redirects=False)
    assert response.status_code == 303 and app.state.db.latest_training_plan() is None


def test_save_deduplicates_preserves_versions_and_separates_proposals(tmp_path):
    client, app = signed_in(tmp_path)
    app.state.model_factory = lambda: pytest.fail('Saving a prepared proposal must not call a model')
    app.state.client_factory = lambda: pytest.fail('Saving a prepared proposal must not call Hevy')
    first = client.post('/plan/draft', json=example_plan())
    assert first.json() == {'status': 'draft', 'version': 1, 'url': '/plan', 'hevy_written': False}
    assert client.post('/plan/draft', json=example_plan()).json()['version'] == 1
    changed = example_plan(); changed['days'][0]['name'] = 'Renamed day'
    assert client.post('/plan/draft', json=changed).json()['version'] == 2
    for index in range(25):
        app.state.db.save_coach_reply(str(index), 'answered', 'A chat question', {'message': 'A reply'})
    assert app.state.db.latest_training_plan()['plan_version'] == 2
    assert app.state.db.count_active() == 0
    page = client.get('/plan')
    assert page.status_code == 200 and 'Renamed day' in page.text
    assert '2 working sets × 8–12 reps' in page.text
    assert 'Trial load: 10 kg' in page.text
    assert 'not completed workouts' in page.text
    assert 'View my weekly plan' in client.get('/coach').text
    tool = next(t for t in _tools(app.state.db, app.state.settings) if t.name == 'current_training_plan')
    value = json.loads(tool.invoke({'day': 'Monday'}))
    assert value['record_type'] == 'proposed_prescription_not_completed_training'
    assert value['days'][0]['exercises'][0]['sets'] == 2
    assert value['days'][0]['exercises'][0]['load_display'] == '10 kg'
    assert not json.loads(tool.invoke({'day': 'Tuesday'}))['found']


@pytest.mark.parametrize('case', ['negative_load', 'reverse_reps', 'duplicate_day', 'javascript_source', 'unknown_field'])
def test_invalid_plan_is_rejected_before_storage(tmp_path, case):
    client, app = signed_in(tmp_path)
    payload = example_plan()
    if case == 'negative_load': payload['days'][0]['exercises'][0]['weight_kg'] = -1
    if case == 'reverse_reps': payload['days'][0]['exercises'][0]['reps_min'] = 20
    if case == 'duplicate_day': payload['days'].append(deepcopy(payload['days'][0]))
    if case == 'javascript_source': payload['sources'][0]['url'] = 'javascript:alert(1)'
    if case == 'unknown_field': payload['write_to_hevy'] = True
    assert client.post('/plan/draft', json=payload).status_code == 422
    assert app.state.db.latest_training_plan() is None
