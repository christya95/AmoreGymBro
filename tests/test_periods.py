from datetime import datetime, timezone

import pytest

from app.periods import history_period
from tests.test_app import signed_in

NOW = datetime(2026, 10, 31, 16, tzinfo=timezone.utc)


def test_calendar_periods_use_toronto_midnight_and_monday_weeks():
    today = history_period('today', now=NOW)
    week = history_period('week', now=NOW)
    month = history_period('month', now=NOW)
    assert today['start'] == '2026-10-31T04:00:00Z'
    assert today['end'] == '2026-11-01T04:00:00Z'
    assert week['start'] == '2026-10-26T04:00:00Z'
    assert week['end'] == '2026-11-02T05:00:00Z'
    assert month['start'] == '2026-10-01T04:00:00Z'
    assert month['end'] == '2026-11-01T04:00:00Z'
    assert 'America/Toronto' in today['caption']


def test_daylight_saving_day_uses_25_hour_calendar_day():
    period = history_period('today', now=datetime(2026, 11, 1, 18, tzinfo=timezone.utc))
    assert period['start'] == '2026-11-01T04:00:00Z'
    assert period['end'] == '2026-11-02T05:00:00Z'


def test_december_rollover_and_unknown_filter():
    period = history_period('month', now=datetime(2026, 12, 31, 12, tzinfo=timezone.utc))
    assert period['end'] == '2027-01-01T05:00:00Z'
    assert history_period('invalid', now=NOW)['key'] == 'all'
    assert history_period('all', now=NOW)['start'] is None


def seed(db, wid, when, weight=10, *, empty=False):
    db.apply_workout({
        'id': wid, 'title': wid, 'start_time': when,
        'updated_at': '2026-10-31T16:00:00Z',
        'exercises': [] if empty else [{
            'index': 0, 'title': 'Bench', 'exercise_template_id': 'BENCH',
            'sets': [
                {'index': 0, 'type': 'normal', 'weight_kg': weight, 'reps': 5},
                {'index': 1, 'type': 'warmup', 'weight_kg': 5, 'reps': 10},
                {'index': 2, 'type': 'normal', 'weight_kg': None, 'reps': 5},
            ],
        }],
    }, '2026-10-31T16:00:00Z')


@pytest.mark.parametrize('key,count,volume', [('today', 2, 50), ('week', 3, 150), ('month', 4, 300), ('all', 6, 750)])
def test_totals_counts_and_records_share_period(tmp_path, key, count, volume):
    _, app = signed_in(tmp_path)
    db = app.state.db
    seed(db, 'today', '2026-10-31T04:00:00Z', 10)
    seed(db, 'week', '2026-10-27T12:00:00Z', 20)
    seed(db, 'month', '2026-10-10T12:00:00Z', 30)
    seed(db, 'old', '2026-09-30T12:00:00Z', 40)
    seed(db, 'undated', None, 50)
    seed(db, 'empty', '2026-10-31T12:00:00Z', empty=True)
    seed(db, 'deleted', '2026-10-31T12:00:00Z', 999)
    db.mark_deleted('deleted', '2026-11-01T16:00:00Z', '2026-11-01T16:00:00Z')
    period = history_period(key, now=NOW)
    from app.main import _history_bundle
    exercises, totals, _ = _history_bundle(db, period['start'], period['end'])
    assert totals['confirmed_workout_count'] == count
    assert totals['total_volume_kg'] == volume
    assert totals['working_set_count'] == (count - 1) * 2
    assert totals['sets_missing_load'] == count - 1
    assert len(exercises[0]['sessions']) == count - 1
    assert exercises[0]['personal_record']['weight_kg'] == {'today': 10, 'week': 20, 'month': 30, 'all': 50}[key]


def test_exact_boundaries_and_offset_timestamps(tmp_path):
    _, app = signed_in(tmp_path)
    db = app.state.db
    seed(db, 'before', '2026-10-31T03:59:59Z')
    seed(db, 'start', '2026-10-31T00:00:00-04:00')
    seed(db, 'last', '2026-11-01T03:59:59Z')
    seed(db, 'end', '2026-11-01T04:00:00Z')
    period = history_period('today', now=NOW)
    rows = db.confirmed_exercise_rows(period['start'], period['end'])
    assert {r['workout_id'] for r in rows} == {'start', 'last'}
    assert db.count_active(period['start'], period['end']) == 2


def test_routes_preserve_filter_and_allow_empty_exercise_period(tmp_path, monkeypatch):
    import app.main as module
    monkeypatch.setattr(module, 'history_period', lambda value, **kwargs: history_period(value, now=NOW))
    client, app = signed_in(tmp_path)
    seed(app.state.db, 'older', '2026-10-10T12:00:00Z', 30)
    page = client.get('/history?period=month')
    assert page.status_code == 200
    assert 'type="range"' in page.text
    assert "This month&#39;s totals" in page.text
    assert '&amp;period=month' in page.text
    assert '150 kg' in page.text
    assert 'history-period.js' in page.text
    detail = client.get('/history/exercise', params={'key': 'id:BENCH', 'period': 'today'})
    assert detail.status_code == 200
    assert 'No sessions in this period' in detail.text
    assert '30 kg' not in detail.text
    assert '/history?period=today' in detail.text
    page = client.get('/history?period=today')
    assert 'No confirmed exercises in this period' in page.text
    assert 'Not calculated' in page.text
    assert '150 kg' not in page.text
    assert 'No confirmed workouts were saved in this period.' in page.text
    assert 'No confirmed Hevy workouts are stored.' not in page.text
