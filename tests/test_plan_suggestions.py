from copy import deepcopy
import json
import re

import pytest
from langchain_core.messages import AIMessage

from app.coach import ask
from app.plan_suggestions import (prepare_suggestion, record_suggestion, decide_suggestion,
                                  suggestion_state, SuggestionBlocked)
from app.training_plan import WeeklyPlan, save_plan_proposal
from tests.test_app import signed_in
from tests.test_coaching import RecordingModel
from tests.test_routine import _seed_complete
from tests.test_training_plan import example_plan

NOW = "2026-11-01T12:00:00Z"
CATALOG = [{"title": "Machine press", "id": "press", "type": "weight_reps"},
           {"title": "Push Up", "id": "pushup", "type": "reps_only"}]
PATCH = {"day": "Monday", "exercise": "Machine press", "weight_kg": 12.5,
         "reason": "A small proposed step if your current load feels controlled."}


def seeded(tmp_path):
    client, app = signed_in(tmp_path, openai_api_key="fake-provider")
    _seed_complete(app)
    saved = save_plan_proposal(app.state.db, WeeklyPlan.model_validate(example_plan()), NOW)
    (tmp_path / 'hevy-exercise-catalog.json').write_text(json.dumps(CATALOG))
    return client, app, saved


def proposal_for(app, saved, patch=PATCH):
    return prepare_suggestion(app.state.db, saved, [patch], CATALOG)


def token(client):
    html = client.get('/coach').text
    # CSRF exists only on the conditional suggestion form.
    return re.search(r'name="csrf_token" value="([^"]+)"', html)[1]


def show_proposal(app, proposal):
    record_suggestion(app.state.db, proposal, NOW)
    app.state.db.save_coach_reply(NOW, 'answered', 'Can we adjust Monday?', {
        'message': 'Big Dawg, here’s the proposed tweak.',
        'plan_suggestion': {k: proposal[k] for k in ('suggestion_id', 'changes')}})


@pytest.mark.parametrize('decision', ['apply', 'dismiss'])
def test_chat_choice_is_conditional_explicit_and_local_only(tmp_path, decision):
    client, app, saved = seeded(tmp_path)
    assert 'Update my plan' not in client.get('/coach').text
    assert 'Update my plan' not in client.get('/plan').text
    assert 'Save changes to Hevy' in client.get('/plan').text
    proposal = proposal_for(app, saved)
    show_proposal(app, proposal)
    app.state.model_factory = lambda: pytest.fail('Approval may not call the model')
    app.state.routine_api_factory = lambda: pytest.fail('Local approval may not call Hevy')
    html = client.get('/coach').text
    assert html.index('Current:') < html.index('Proposed:') < html.index('Update my plan')
    endpoint = '/coach/plan/' + proposal['suggestion_id']
    assert client.post(endpoint, data={'decision': decision}).status_code == 403
    csrf = token(client)
    accepted = client.post(endpoint, data={'decision': decision, 'csrf_token': csrf})
    assert accepted.status_code == 200
    expected = 2 if decision == 'apply' else 1
    assert app.state.db.latest_training_plan()['plan_version'] == expected
    assert 'Update my plan' not in accepted.text
    assert ('Plan updated in this app' if decision == 'apply' else 'You kept your current plan') in accepted.text
    # A double submission never creates another version.
    client.post(endpoint, data={'decision': decision, 'csrf_token': csrf})
    assert app.state.db.latest_training_plan()['plan_version'] == expected
    assert not app.state.db.publication_events()


def test_stale_unknown_and_anonymous_choices_cannot_apply(tmp_path):
    client, app, saved = seeded(tmp_path)
    proposal = proposal_for(app, saved); show_proposal(app, proposal)
    csrf = token(client)
    changed = example_plan(); changed['phase'] = 'A newer phase'
    save_plan_proposal(app.state.db, WeeklyPlan.model_validate(changed), NOW)
    response = client.post('/coach/plan/' + proposal['suggestion_id'], data={'decision': 'apply', 'csrf_token': csrf})
    assert 'your plan has changed' in response.text
    assert app.state.db.latest_training_plan()['plan_version'] == 2
    assert client.post('/coach/plan/unknown', data={'decision': 'apply', 'csrf_token': csrf}).status_code == 200
    client.cookies.clear()
    assert client.post('/coach/plan/unknown', data={'decision': 'apply'}, follow_redirects=False).status_code == 303


@pytest.mark.parametrize('changes', [
    [{**PATCH, 'day': 'Friday'}],
    [{**PATCH, 'exercise': 'Imaginary exercise'}],
    [PATCH, PATCH],
    [{**PATCH, 'replacement': 'Unknown', 'load_basis': 'machine_display'}],
    [{**PATCH, 'replacement': 'Push Up', 'load_basis': 'machine_display'}],
    [{**PATCH, 'replacement': 'Machine press', 'weight_kg': None, 'load_basis': 'bodyweight'}],
])
def test_invalid_patch_never_changes_saved_plan(tmp_path, changes):
    _, app, saved = seeded(tmp_path)
    with pytest.raises((SuggestionBlocked, ValueError)):
        prepare_suggestion(app.state.db, saved, changes, CATALOG)
    assert app.state.db.latest_training_plan()['plan_fingerprint'] == saved['plan_fingerprint']


def test_swap_does_not_inherit_old_load_or_note(tmp_path):
    _, app, saved = seeded(tmp_path)
    change = {'day': 'Monday', 'exercise': 'Machine press', 'replacement': 'Push Up',
              'load_basis': 'bodyweight', 'sets': 2, 'reps_min': 5, 'reps_max': 8,
              'reason': 'An equipment-free alternative.'}
    proposal = proposal_for(app, saved, change)
    item = proposal['candidate']['days'][0]['exercises'][0]
    assert item['weight_kg'] is None and item['name'] == 'Push Up'
    assert item['reference'] != saved['weekly_plan']['days'][0]['exercises'][0]['reference']
    assert app.state.db.latest_training_plan()['plan_version'] == 1


def test_suggestion_ledger_survives_retention_and_is_not_a_chat_reply(tmp_path):
    _, app, saved = seeded(tmp_path)
    proposal = proposal_for(app, saved); record_suggestion(app.state.db, proposal, NOW)
    decide_suggestion(app.state.db, proposal['suggestion_id'], 'dismiss', NOW)
    for i in range(25): app.state.db.save_coach_reply(NOW, 'answered', str(i), {'message': 'chat'})
    assert suggestion_state(app.state.db, proposal['suggestion_id'])[1] == 'dismissed'
    assert len(app.state.db.plan_suggestion_events()) == 2
    assert app.state.db.latest_coach_reply()['question'] == '24'


def script(with_lookup=True):
    calls = [{'name': 'training_profile', 'args': {}, 'id': 'profile', 'type': 'tool_call'}]
    if with_lookup: calls.append({'name': 'current_training_plan', 'args': {'day': 'Monday'}, 'id': 'plan', 'type': 'tool_call'})
    return RecordingModel(messages=iter([
        AIMessage(content='', tool_calls=calls),
        AIMessage(content='', tool_calls=[{'name': 'CoachReply', 'id': 'reply', 'type': 'tool_call', 'args': {
            'observed_facts': ['Your saved plan includes a machine press.'],
            'suggested_action': 'Big Dawg, review this small trial tweak before we touch the plan. 💪',
            'supporting_workout_ids': [], 'uncertainty': 'I cannot see exercise form or diagnose injuries.',
            'missing_information': [], 'plan_changes': [PATCH]}}])]))


def test_normal_chat_generates_typed_proposal_without_extra_extraction_call(tmp_path):
    client, app, _ = seeded(tmp_path)
    model = script(); app.state.model_factory = lambda: model
    response = client.post('/coach', data={'question': 'Suggest a small Monday press change.'})
    assert 'Update my plan' in response.text and '12.5 kg' in response.text
    assert len(model.seen) == 2  # Existing tool lookup + structured answer, no extraction/style call.
    assert app.state.db.latest_training_plan()['plan_version'] == 1
    assert len(app.state.db.plan_suggestion_events()) == 1


def test_proposal_requires_relevant_plan_lookup_and_complete_intake(tmp_path):
    _, app, _ = seeded(tmp_path)
    reply = ask(app.state.db, app.state.settings, 'Change Monday', model_factory=lambda: script(False))
    assert reply['prepared_plan_suggestion'] is None and reply['proposal_notice']
    app.state.db.save_body_entry(height_cm=None, age_years=31, sex='male', activity='sedentary', updated_at=NOW, days_per_week=5, session_minutes=60, train_where='gym')
    reply = ask(app.state.db, app.state.settings, 'Change Monday', model_factory=lambda: script())
    assert reply['status'] == 'missing_information' and 'height' in reply['missing_information']
