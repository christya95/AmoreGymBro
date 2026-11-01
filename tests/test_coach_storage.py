import json

from app.coach import _profile_payload, _tools, _workout_payload, DETAIL_SET_CAP
from app.macros import macro_guide
from app.present import format_weight
from tests.test_app import signed_in, KEY
from tests.test_routine import _seed_complete


def test_profile_retrieval_exposes_calculated_macros_and_provenance(tmp_path):
    _, app = signed_in(tmp_path)
    _seed_complete(app)
    app.state.db.save_profile({'weight_unit': 'lbs'}, '2026-11-01T00:00:00Z')
    profile = _profile_payload(app.state.db)
    assert profile['saved_weight']['record_type'] == 'confirmed_hevy'
    assert profile['saved_weight']['weight_kg'] == 84
    assert profile['saved_weight']['display'] == format_weight(84, 'lbs')
    assert profile['body_and_training']['record_type'] == 'user_report'
    assert profile['body_and_training']['days_per_week'] == 4
    expected = macro_guide(weight_kg=84, height_cm=180, age_years=35, sex='male', activity='moderate')
    assert profile['daily_macro_guide']['calories'] == expected['calories']
    assert profile['daily_macro_guide']['protein_g'] == expected['protein_g']
    assert profile['daily_macro_guide']['record_type'] == 'app_calculated_estimate'


def test_single_workout_retrieval_is_bounded_redacted_and_read_only(tmp_path):
    _, app = signed_in(tmp_path, hevy_api_key=KEY)
    source = {
        'id': 'many-sets', 'title': 'Long session', 'description': KEY,
        'start_time': '2026-11-01T00:00:00Z', 'updated_at': '2026-11-01T01:00:00Z',
        'exercises': [{'index': index, 'title': 'Exercise', 'notes': KEY,
                       'sets': [{'index': number, 'type': 'normal', 'weight_kg': 10, 'reps': 5}
                                for number in range(10)]} for index in range(10)],
    }
    app.state.db.apply_workout(source, '2026-11-01T01:00:00Z')
    before = app.state.db.get_workout('many-sets')
    tools = {tool.name: tool for tool in _tools(app.state.db, app.state.settings)}
    assert set(tools) == {'recent_workouts', 'exercise_history', 'training_profile', 'workout_details', 'current_training_plan', 'exercise_options', 'garmin_recovery'}
    text = tools['workout_details'].invoke({'workout_id': 'many-sets'})
    payload = json.loads(text)
    assert KEY not in text
    assert 'raw_json' not in text and 'mongodb://' not in text
    assert sum(len(item['sets']) for item in payload['exercises']) == DETAIL_SET_CAP
    assert payload['sets_omitted'] == 76
    assert payload['exercises_omitted'] == 4
    assert payload['totals']['total_volume_kg'] == 5000
    assert app.state.db.get_workout('many-sets') == before
    assert _workout_payload(app.state.db, {'$ne': None}) == {'found': False}
    assert not _workout_payload(app.state.db, 'missing')['found']


def test_missing_profile_inputs_stay_missing_in_retrieval(tmp_path):
    _, app = signed_in(tmp_path)
    payload = _profile_payload(app.state.db)
    assert payload['saved_weight']['weight_kg'] is None
    assert payload['body_and_training']['height_cm'] is None
    assert payload['daily_macro_guide']['calories'] is None
    assert not payload['daily_macro_guide']['complete']
