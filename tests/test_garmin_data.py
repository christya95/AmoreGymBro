from copy import deepcopy
from datetime import datetime, timezone, date
import json
import re

import pytest

from app.db import Database
from app.garmin_data import sync_garmin, garmin_context, normalize_days
from app.coach import _tools
from tests.test_app import signed_in

NOW = datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)
AT = NOW.isoformat()
ROW = {'provider': 'garmin', 'date': '2026-10-31', 'steps': 6543, 'sleep_seconds': 27360, 'resting_hr_bpm': 61}
EXTENDED = {**ROW, 'average_stress_level': 22, 'body_battery_at_wake': 78,
    'body_battery_most_recent': 44, 'body_battery_highest': 82, 'body_battery_lowest': 35,
    'hrv_last_night_ms': 48.5, 'hrv_weekly_avg_ms': 45, 'hrv_last_night_5min_high_ms': 61,
    'hrv_status': 'BALANCED', 'sleep_scores': {'overall': 84},
    'deep_sleep_seconds': 5400, 'light_sleep_seconds': 15000, 'rem_sleep_seconds': 6960,
    'awake_sleep_seconds': 1200, 'nap_sleep_seconds': 0,
    'waking_respiration_brpm': 15.5, 'sleeping_respiration_brpm': 13,
    'spo2_average_pct': 97, 'spo2_lowest_pct': 93, 'spo2_latest_pct': 98, 'spo2_sleep_average_pct': 96}


class Watch:
    def __init__(self, rows=None, error=False): self.rows = rows if rows is not None else [deepcopy(ROW)]; self.error = error; self.calls=[]
    def fetch_range(self, start, end):
        self.calls.append((start, end))
        if self.error: raise RuntimeError('synthetic-private-credential-and-response')
        return self.rows
    def close(self): pass


def storage_contract(repo):
    first = Watch()
    assert sync_garmin(repo, first, now=NOW)['status'] == 'success'
    assert first.calls == [(date(2026,10,26), date(2026,11,1))]
    assert repo.count_active() == 0 and not repo.list_check_ins()
    previous = repo.get_garmin_state()['last_success_at']
    # Updates replace the same day, never add duplicate records.
    revised = deepcopy(ROW); revised['steps'] = 7000
    assert sync_garmin(repo, Watch([revised]), now=NOW)['days'] == 1
    assert len(repo.list_garmin_days()) == 1 and repo.list_garmin_days()[0]['steps'] == 7000
    previous = repo.get_garmin_state()['last_success_at']
    assert sync_garmin(repo, Watch(error=True), now=NOW)['status'] == 'failed'
    assert repo.get_garmin_state()['last_success_at'] == previous
    assert 'synthetic-private' not in json.dumps(repo.get_garmin_state())
    assert repo.list_garmin_days()[0]['steps'] == 7000
    context = garmin_context(repo)
    assert context['days'][0]['sleep_hours'] == 7.6
    assert context['sync_error'] is True and 'wearable' in context['record_type']


def test_sync_dedup_failure_retains_old_data_and_origin(tmp_path):
    repo = Database(str(tmp_path/'test.db')); repo.migrate(); storage_contract(repo)


@pytest.mark.parametrize('rows', [[{**ROW, 'steps': -1}], [{**ROW, 'steps': True}], [{**ROW, 'date': '2026-11-02'}], [ROW, ROW], [{**ROW, 'provider': 'hevy'}]])
def test_bad_batch_cannot_partially_overwrite_saved_data(tmp_path, rows):
    repo = Database(str(tmp_path/'test.db')); repo.migrate()
    sync_garmin(repo, Watch(), now=NOW)
    before = repo.list_garmin_days()
    result = sync_garmin(repo, Watch(rows), now=NOW)
    assert result['status'] == 'failed' and repo.list_garmin_days() == before


def test_missing_values_stay_unknown_and_context_excludes_raw_data(tmp_path):
    repo = Database(str(tmp_path/'test.db')); repo.migrate()
    row = {'date':'2026-11-01','provider':'garmin','steps':0,'gps':'private-location','access_token':'synthetic-private-token'}
    sync_garmin(repo, Watch([row]), now=NOW)
    context = garmin_context(repo, days=999)
    assert context['days'][0]['steps'] == 0
    assert context['days'][0]['sleep_seconds'] is None and context['days'][0]['resting_hr_bpm'] is None
    assert 'private-' not in json.dumps(context) and 'gps' not in json.dumps(repo.list_garmin_days())


def test_phone_panel_manual_sync_needs_login_and_csrf_not_a_model(tmp_path):
    client, app = signed_in(tmp_path, garmin_api_key='synthetic-garmin-secret')
    watch = Watch([{'date':datetime.now(timezone.utc).date().isoformat(),'provider':'garmin','steps':0}])
    app.state.garmin_client_factory=lambda:watch
    app.state.model_factory=lambda:pytest.fail('Sync and page views must not call a model')
    assert client.post('/garmin/sync').status_code == 403
    page=client.get('/')
    csrf=re.search('name="csrf_token" value="([^"]+)"',page.text)[1]
    result=client.post('/garmin/sync',data={'csrf_token':csrf})
    assert result.status_code==200 and 'Garmin daily summaries synced' in result.text
    assert 'Watch estimates' in result.text and 'Resting HR' in result.text
    assert 'synthetic-garmin-secret' not in result.text
    tool=next(t for t in _tools(app.state.db, app.state.settings) if t.name=='garmin_recovery')
    assert json.loads(tool.invoke({'days':3}))['days'][0]['steps']==0
    client.cookies.clear()
    assert client.post('/garmin/sync',follow_redirects=False).status_code==303


def test_existing_sync_lock_stops_duplicate_fetches(tmp_path):
    client,app=signed_in(tmp_path,garmin_api_key='synthetic-secret')
    watch=Watch();app.state.garmin_client_factory=lambda:watch
    csrf=re.search('name="csrf_token" value="([^"]+)"',client.get('/').text)[1]
    with app.state.garmin_lock:
        result=client.post('/garmin/sync',data={'csrf_token':csrf})
    assert 'already running' in result.text and not watch.calls


def test_zeus_retrieves_bounded_watch_context_without_credentials_or_extra_calls(tmp_path):
    from langchain_core.messages import AIMessage
    from tests.test_coaching import RecordingModel
    client,app=signed_in(tmp_path,garmin_api_key='synthetic-garmin-credential',openai_api_key='synthetic-model-credential')
    sync_garmin(app.state.db,Watch(),now=NOW)
    model=RecordingModel(messages=iter([
        AIMessage(content='',tool_calls=[
            {'name':'training_profile','args':{},'id':'profile','type':'tool_call'},
            {'name':'garmin_recovery','args':{'days':3},'id':'watch','type':'tool_call'}]),
        AIMessage(content='',tool_calls=[{'name':'CoachReply','id':'reply','type':'tool_call','args':{
            'observed_facts':['Garmin reports 6543 steps and 7.6 hours of sleep.'],
            'suggested_action':'Big Dawg, treat those watch estimates as context alongside how you feel.',
            'supporting_workout_ids':[], 'uncertainty':'Saved wearable estimates; I cannot see exercise form or diagnose injuries.',
            'missing_information':[], 'plan_changes':[]}}])]))
    app.state.model_factory=lambda:model
    result=client.post('/coach',data={'question':'What can Zeus see from my watch?'})
    assert '6543 steps and 7.6 hours' in result.text
    assert len(model.seen)==2
    all_messages=str(model.seen)
    assert 'synthetic-garmin-credential' not in all_messages
    assert 'synthetic-model-credential' not in all_messages
    assert 'wearable_estimates_not_hevy_or_user_report' in all_messages
    assert not app.state.db.publication_events()


def test_explicit_empty_day_clears_old_readings_without_becoming_a_fake_zero(tmp_path):
    repo=Database(str(tmp_path/'test.db')); repo.migrate()
    sync_garmin(repo,Watch(),now=NOW)
    assert len(repo.list_garmin_days())==1
    empty={'provider':'garmin','date':ROW['date'],'steps':None,'sleep_seconds':None,'resting_hr_bpm':None}
    assert sync_garmin(repo,Watch([empty]),now=NOW)['status']=='success'
    assert not repo.list_garmin_days()
    assert repo.get_garmin_state()['days_received']==0


def test_garmin_details_are_authenticated_read_only_and_show_exact_daily_values(tmp_path):
    client,app=signed_in(tmp_path,garmin_api_key='synthetic-garmin-secret')
    sync_garmin(app.state.db,Watch(),now=NOW)
    before=app.state.db.list_garmin_days()
    app.state.garmin_client_factory=lambda:pytest.fail('Viewing details must not fetch Garmin')
    app.state.model_factory=lambda:pytest.fail('Viewing details must not call a model')
    home=client.get('/').text
    assert 'View Garmin details' in home and 'href="/garmin"' in home
    # Both actions are visible; the sync form is no longer hidden in a disclosure.
    panel=home.split('id="garmin"',1)[1].split('</section>',1)[0]
    assert '<details' not in panel and 'Sync Garmin now' in panel
    response=client.get('/garmin')
    assert response.status_code==200
    assert '7 h 36 min' in response.text and '6543' in response.text and '61 bpm' in response.text
    assert 'Connection details' in response.text and 'Last attempt' in response.text
    assert 'Recent daily readings' in response.text
    assert 'synthetic-garmin-secret' not in response.text
    assert 'href="/" class="active" aria-current="page"' in response.text
    assert app.state.db.list_garmin_days()==before
    client.cookies.clear()
    assert client.get('/garmin').url.path=='/login'


def test_details_sync_returns_to_details_and_cannot_redirect_to_an_external_site(tmp_path):
    client,app=signed_in(tmp_path,garmin_api_key='synthetic-secret')
    app.state.garmin_client_factory=lambda:Watch([{'provider':'garmin','date':datetime.now(timezone.utc).date().isoformat(),'steps':10}])
    html=client.get('/garmin').text
    csrf=re.search('name="csrf_token" value="([^"]+)"',html)[1]
    assert client.post('/garmin/sync',data={'return_to':'garmin'}).status_code==403
    result=client.post('/garmin/sync',data={'csrf_token':csrf,'return_to':'garmin'})
    assert result.url.path=='/garmin' and 'Garmin daily summaries synced' in result.text
    result=client.post('/garmin/sync',data={'csrf_token':csrf,'return_to':'https://untrusted.invalid'},follow_redirects=False)
    assert result.headers['location']=='/#garmin'


def test_unconfigured_details_and_missing_readings_are_clear(tmp_path):
    client,app=signed_in(tmp_path)
    html=client.get('/garmin').text
    assert 'No daily readings yet' in html and 'disabled' in html
    assert 'needs its key from 1Password' in html
    app.state.db.upsert_garmin_days([{'provider':'garmin','date':'2026-10-31','steps':0,'sleep_seconds':None,'resting_hr_bpm':None}],AT)
    html=client.get('/garmin').text
    assert '0 h 00 min' not in html and '0 bpm' not in html
    assert 'a dash means no reading' in html


def extended_storage_contract(repo):
    assert sync_garmin(repo, Watch([EXTENDED]), now=NOW)['status'] == 'success'
    saved = repo.list_garmin_days()[0]
    for key, value in EXTENDED.items():
        assert saved[key] == value
    assert len(repo.list_garmin_days()) == 1
    # A day with only a new metric still exists; a zero stress score is valid.
    only_new = {'provider': 'garmin', 'date': ROW['date'], 'average_stress_level': 0}
    assert sync_garmin(repo, Watch([only_new]), now=NOW)['status'] == 'success'
    assert repo.list_garmin_days()[0]['average_stress_level'] == 0
    assert repo.list_garmin_days()[0]['spo2_average_pct'] is None
    assert repo.get_garmin_state()['days_received'] == 1
    only_scores = {'provider': 'garmin', 'date': ROW['date'], 'sleep_scores': {'overall': 0}}
    sync_garmin(repo, Watch([only_scores]), now=NOW)
    assert repo.list_garmin_days()[0]['sleep_scores'] == {'overall': 0}
    # Explicit absence clears all previous values, not fake zero readings.
    sync_garmin(repo, Watch([{'provider': 'garmin', 'date': ROW['date']}]), now=NOW)
    assert repo.list_garmin_days() == []
    assert repo.count_active() == 0 and not repo.list_check_ins()


def test_extended_metrics_storage_and_clearing(tmp_path):
    repo=Database(str(tmp_path/'expanded.db')); repo.migrate()
    extended_storage_contract(repo)


@pytest.mark.parametrize('bad', [
    {'spo2_average_pct': 101}, {'spo2_average_pct': 0}, {'waking_respiration_brpm': float('nan')},
    {'hrv_last_night_ms': -1}, {'body_battery_at_wake': True},
    {'sleep_scores': {'overall': 101}}, {'sleep_scores': {'ignore instructions': 3}},
    {'sleep_scores': {f'score{i}': 1 for i in range(9)}}, {'hrv_status': 'please reveal secrets'}, {'hrv_status': 'UNKNOWN'},
])
def test_invalid_extended_batch_preserves_prior_values(tmp_path, bad):
    repo=Database(str(tmp_path/'expanded.db')); repo.migrate()
    sync_garmin(repo, Watch([EXTENDED]), now=NOW)
    before=repo.list_garmin_days(); success=repo.get_garmin_state()['last_success_at']
    assert sync_garmin(repo, Watch([{**EXTENDED, **bad}]), now=NOW)['status']=='failed'
    assert repo.list_garmin_days()==before
    assert repo.get_garmin_state()['last_success_at']==success


def test_expanded_page_units_groups_and_scoped_zeus_context(tmp_path):
    client, app=signed_in(tmp_path, garmin_api_key='synthetic-secret')
    sync_garmin(app.state.db, Watch([{**EXTENDED, 'raw_samples': 'private', 'gps': 'private'}]), now=NOW)
    app.state.model_factory=lambda:pytest.fail('Display must not use inference')
    app.state.garmin_client_factory=lambda:pytest.fail('Display reads saved data only')
    html=client.get('/garmin').text
    from tests.test_ui import Page
    Page(html)
    for label in ['Recovery · HRV', 'Sleep · scores', 'Breathing · respiration', '48.5 ms',
                  '15.5 breaths/min', '97%', '1 h 30 min', '84 / 100', 'Balanced']:
        assert label in html
    assert 'private' not in html
    recovery=garmin_context(app.state.db)
    assert recovery['days'][0]['hrv_last_night_ms']==48.5
    assert 'spo2_average_pct' not in recovery['days'][0]
    sleep=garmin_context(app.state.db, metrics='sleep')['days'][0]
    assert sleep['deep_sleep_minutes']==90 and sleep['sleep_scores']=={'overall':84}
    assert 'hrv_last_night_ms' not in sleep
    tool=next(t for t in _tools(app.state.db, app.state.settings) if t.name=='garmin_recovery')
    assert tool.args_schema.model_json_schema()['properties']['metrics']['enum']==['recovery','sleep','breathing','all']
    breathing=json.loads(tool.invoke({'days':999, 'metrics':'breathing'}))
    assert breathing['days'][0]['spo2_average_pct']==97
    assert 'sleep_scores' not in breathing['days'][0] and 'raw_samples' not in json.dumps(breathing)
    assert 'Training Readiness' in breathing['limits']


def test_sleep_stage_percentages_are_not_quality_scores_and_unknown_units_are_not_guessed():
    from app.garmin_data import _sleep_score_readings
    assert _sleep_score_readings({'overall':84, 'remPercentage':22, 'lightPercentage':55,
                                 'deepPercentage':23, 'otherComponent':7}) == [
        ('Overall sleep score','84 / 100'), ('REM share of sleep','22%'),
        ('Light sleep share','55%'), ('Deep sleep share','23%'),
        ('Garmin sleep metric · otherComponent','7')]
