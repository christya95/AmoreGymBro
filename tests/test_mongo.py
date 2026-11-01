"""Run against a real, authenticated local MongoDB with RUN_MONGODB_TESTS=1."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database, open_database
from app.macros import PRIMARY_GOAL
from app.migrate_mongo import MigrationError, backup_sqlite, import_snapshot, sqlite_snapshot, verify
from app.mongo import MongoDatabase
from app.timeutil import to_iso
from tests.test_app import PASSWORD, settings
from tests.test_sync import (
    FakeHevy, START, END, workout, synced,
    test_full_sync_records_timestamps_weight_and_read_calls_only,
    test_incremental_sync_applies_newer_delete_and_skips_full_list,
    test_newer_update_restores_a_deleted_workout,
    test_failed_sync_does_not_advance_cursor_and_redacts_secrets,
    test_count_mismatch_stays_visible_after_success,
    test_duplicate_event_is_idempotent,
)

# Import shared contracts under private aliases so pytest doesn't collect their originals here.
_CONTRACTS = [value for key, value in list(globals().items()) if key.startswith('test_')]
for _name in [key for key in list(globals()) if key.startswith('test_')]:
    del globals()[_name]
pytestmark = pytest.mark.skipif(os.environ.get('RUN_MONGODB_TESTS') != '1', reason='Real local MongoDB integration tests are opt-in.')


@pytest.fixture
def mongo():
    database = 'amoregymbro_test_' + uuid4().hex
    repo = MongoDatabase(
        os.environ.get('TEST_MONGODB_URI', 'mongodb://127.0.0.1:27018'), database,
        username=os.environ['TEST_MONGODB_USERNAME'], password=os.environ['TEST_MONGODB_PASSWORD'],
    )
    try:
        repo.migrate()
        yield repo
    finally:
        assert database.startswith('amoregymbro_test_') and len(database) == 49
        repo.client.drop_database(database)
        repo.close()


@pytest.mark.parametrize('contract', _CONTRACTS, ids=lambda function: function.__name__)
def test_shared_sync_contracts_on_real_mongodb(mongo, contract):
    contract(mongo)


def test_nested_workout_matches_sqlite_and_atomic_failure_keeps_old_sets(mongo, tmp_path):
    source = Database(str(tmp_path / 'source.db'))
    source.migrate()
    payload = workout()
    source.apply_workout(payload, to_iso(START))
    mongo.apply_workout(payload, to_iso(START))
    assert mongo.get_workout('w1') == source.get_workout('w1')
    malformed = workout(updated='2026-10-31T22:00:00Z', weight=999)
    malformed['exercises'][0]['sets'].append(dict(malformed['exercises'][0]['sets'][0]))
    with pytest.raises(ValueError, match='Duplicate set'):
        mongo.apply_workout(malformed, to_iso(END))
    assert mongo.get_workout('w1') == source.get_workout('w1')


def test_zeus_reads_confirmed_mongo_document_without_mutation_and_hides_deletion(mongo):
    from app.coach import _workout_payload, _profile_payload
    mongo.apply_workout(workout(), to_iso(START))
    before = mongo.get_workout('w1')
    result = _workout_payload(mongo, 'w1')
    assert result['found'] and result['record_type'] == 'confirmed_hevy'
    assert result['totals']['confirmed_workout_count'] == 1
    assert 'raw_json' not in result
    assert mongo.get_workout('w1') == before
    assert _profile_payload(mongo)['daily_macro_guide']['calories'] is None
    mongo.mark_deleted('w1', '2026-11-01T12:00:00Z', to_iso(END))
    assert not _workout_payload(mongo, 'w1')['found']


def test_concurrent_versions_keep_newest_and_never_duplicate(mongo):
    def save(day):
        return mongo.apply_workout(workout(updated=f'2026-10-{day:02d}T12:00:00Z', weight=day), to_iso(START))
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(save, range(1, 21)))
    assert mongo.count_active() == 1
    assert mongo.get_workout('w1')['exercises'][0]['sets'][0]['weight_kg'] == 20


def test_queue_claim_is_atomic_and_recovers_after_restart(mongo):
    assert mongo.enqueue_webhook_job('w1', to_iso(START)) == 'queued'
    assert mongo.enqueue_webhook_job('w1', to_iso(START)) == 'duplicate'
    with ThreadPoolExecutor(max_workers=6) as executor:
        claims = list(executor.map(lambda _: mongo.claim_due_webhook_jobs(to_iso(END)), range(6)))
    assert sum(len(ids) for ids in claims) == 1
    assert mongo.get_webhook_job('w1')['attempts'] == 1
    assert mongo.recover_running_webhook_jobs(to_iso(END)) == 1
    assert mongo.claim_due_webhook_jobs(to_iso(END)) == ['w1']
    mongo.complete_webhook_job('w1', status='failed', now=to_iso(END), error='offline', next_attempt_at=None)
    assert mongo.enqueue_webhook_job('w1', to_iso(END)) == 'requeued'
    assert mongo.webhook_counts()['pending'] == 1


def seed_source(tmp_path):
    source = Database(str(tmp_path / 'source.db'))
    source.migrate()
    synced(source, FakeHevy(workouts=[workout()], count=1), START, END)
    source.mark_deleted('unknown-deletion', '2026-10-30T12:00:00Z', to_iso(END))
    source.save_goal(PRIMARY_GOAL, to_iso(END))
    source.save_check_in('Fresh', 'A stored user report', to_iso(END))
    source.save_body_entry(height_cm=180, age_years=35, sex='male', activity='moderate',
        days_per_week=4, session_minutes=60, train_where='gym', updated_at=to_iso(END))
    source.upsert_body_measurement({'date': '2026-10-21', 'weight_kg': 84}, to_iso(END))
    source.save_coach_reply(to_iso(END), 'success', 'question', {'message': 'saved reply'})
    source.enqueue_webhook_job('w1', to_iso(START))
    return source


def test_migration_copies_every_field_and_is_repeatable(mongo, tmp_path):
    source = seed_source(tmp_path)
    snapshot = sqlite_snapshot(source.path)
    counts = import_snapshot(snapshot, mongo)
    assert counts['workouts'] == 2
    assert mongo.get_workout('w1') == source.get_workout('w1')
    assert mongo.get_workout('unknown-deletion') == source.get_workout('unknown-deletion')
    assert mongo.get_body_entry() == source.get_body_entry()
    assert mongo.latest_body_measurement() == source.latest_body_measurement()
    assert mongo.get_profile() == source.get_profile()
    assert mongo.get_sync_state() == source.get_sync_state()
    assert mongo.latest_coach_reply() == source.latest_coach_reply()
    assert import_snapshot(snapshot, mongo) == counts
    assert mongo.save_goal('Next goal', to_iso(END)) > source.list_goals()[0]['id']
    assert Path(source.path).is_file()


def test_migration_refuses_nonempty_or_changed_targets_and_detects_corruption(mongo, tmp_path):
    source = seed_source(tmp_path)
    snapshot = sqlite_snapshot(source.path)
    import_snapshot(snapshot, mongo)
    mongo.db.check_ins.update_one({'_id': 1}, {'$set': {'notes': 'changed after import'}})
    with pytest.raises(MigrationError, match='contents differ'):
        verify(snapshot, mongo)
    source.save_goal('Changed source', to_iso(END))
    with pytest.raises(MigrationError, match='different SQLite snapshot'):
        import_snapshot(sqlite_snapshot(source.path), mongo)


def test_migration_never_overwrites_an_unrelated_database(mongo, tmp_path):
    source = seed_source(tmp_path)
    mongo.save_goal('Keep me', to_iso(END))
    with pytest.raises(MigrationError, match='existing data'):
        import_snapshot(sqlite_snapshot(source.path), mongo)
    assert mongo.list_goals()[0]['statement'] == 'Keep me'


def test_interrupted_migration_can_resume_same_snapshot(mongo, tmp_path, monkeypatch):
    from pymongo.synchronous.collection import Collection
    source = seed_source(tmp_path)
    snapshot = sqlite_snapshot(source.path)
    original = Collection.replace_one
    def interrupted(collection, *args, **kwargs):
        if collection.name == 'goals':
            raise RuntimeError('Simulated interrupted import')
        return original(collection, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Collection, 'replace_one', interrupted)
        with pytest.raises(RuntimeError):
            import_snapshot(snapshot, mongo)
    assert mongo.db.migrations.find_one({'_id': 'sqlite-import'})['status'] == 'copying'
    import_snapshot(snapshot, mongo)
    assert mongo.list_goals() == source.list_goals()


def test_sqlite_backup_includes_committed_wal_and_preserves_source(mongo, tmp_path):
    source = seed_source(tmp_path)
    backup = tmp_path / 'backups' / 'before.db'
    backup_sqlite(Path(source.path), backup)
    assert sqlite_snapshot(backup) == sqlite_snapshot(source.path)
    assert backup.stat().st_mode & 0o777 == 0o600
    with pytest.raises(MigrationError, match='already exists'):
        backup_sqlite(Path(source.path), backup)


def test_mongo_period_totals_match_sqlite_and_keep_missing_measurements(mongo, tmp_path):
    source = seed_source(tmp_path)
    import_snapshot(sqlite_snapshot(source.path), mongo)
    from app.main import _history_bundle
    for start, end in [(None, None), ('2024-01-01T15:00:00Z', '2024-01-02T15:00:00Z'), ('2026-10-31T04:00:00Z', '2026-11-01T04:00:00Z')]:
        assert _history_bundle(mongo, start, end) == _history_bundle(source, start, end)
    mongo.upsert_body_measurement({'date': '2026-10-31', 'weight_kg': None}, to_iso(END))
    assert mongo.body_weight_history()[-1]['weight_kg'] == 84
    assert mongo.latest_body_measurement()['weight_kg'] is None


def test_app_uses_mongo_and_incomplete_routine_never_calls_model(mongo, tmp_path):
    from app.main import create_app
    config = settings(tmp_path, database_backend='mongodb',
        mongodb_uri=os.environ.get('TEST_MONGODB_URI', 'mongodb://127.0.0.1:27018'),
        mongodb_database=mongo.db.name, mongodb_username=os.environ['TEST_MONGODB_USERNAME'],
        mongodb_password=os.environ['TEST_MONGODB_PASSWORD'])
    app = create_app(config)
    assert isinstance(app.state.db, MongoDatabase)
    app.state.model_factory = lambda *_: pytest.fail('Model must not run for missing profile data')
    with TestClient(app) as client:
        client.post('/login', data={'password': PASSWORD})
        assert client.post('/goals', data={'statement': 'Stronger'}).status_code == 200
        client.post('/check-ins', data={'feeling': 'Fresh', 'notes': 'Stored in MongoDB'})
        assert mongo.list_check_ins()[0]['notes'] == 'Stored in MongoDB'
        response = client.post('/coach/routine')
        assert 'Still needed' in response.text
        assert 'type="range"' in client.get('/history?period=week').text


def test_reply_retention_and_counters_are_atomic(mongo):
    for number in range(25):
        mongo.save_coach_reply(to_iso(END), 'success', str(number), {'message': 'reply'})
    assert mongo.db.coach_replies.count_documents({}) == 20
    assert mongo.latest_coach_reply()['question'] == '24'


def test_plan_versions_survive_chat_retention_on_mongodb(mongo):
    from app.training_plan import WeeklyPlan, save_plan_proposal
    from tests.test_training_plan import example_plan
    plan = example_plan()
    first = save_plan_proposal(mongo, WeeklyPlan.model_validate(plan), to_iso(START))
    assert first['plan_version'] == 1
    assert save_plan_proposal(mongo, WeeklyPlan.model_validate(plan), to_iso(END))['plan_version'] == 1
    plan['days'][0]['name'] = 'A revised day'
    assert save_plan_proposal(mongo, WeeklyPlan.model_validate(plan), to_iso(END))['plan_version'] == 2
    for number in range(25):
        mongo.save_coach_reply(to_iso(END), 'answered', str(number), {'message': 'chat'})
    assert mongo.latest_training_plan()['weekly_plan']['days'][0]['name'] == 'A revised day'
    assert mongo.db.coach_replies.count_documents({'status': 'plan_draft'}) == 2
    assert mongo.db.coach_replies.count_documents({'status': 'answered'}) == 20


def test_hevy_publication_audit_is_permanent_and_not_chat(mongo, monkeypatch, tmp_path):
    from app.training_plan import WeeklyPlan, save_plan_proposal
    from app.hevy.routine_publish import publish_initial, publication_summary
    from tests.test_training_plan import example_plan
    from tests.test_routine_publish import FakeAPI
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'data.db'))
    saved = save_plan_proposal(mongo, WeeklyPlan.model_validate(example_plan()), to_iso(START))
    catalog = [{'title': 'Machine press', 'id': 'verified-template', 'type': 'weight_reps'}]
    result = publish_initial(mongo, FakeAPI(), catalog, approved_fingerprint=saved['plan_fingerprint'], approval_note='Explicit test approval')
    assert result['complete'] and mongo.latest_coach_reply()['status'] == 'plan_draft'
    count = len(mongo.publication_events())
    for number in range(25): mongo.save_coach_reply(to_iso(END), 'answered', str(number), {'message': 'chat'})
    assert len(mongo.publication_events()) == count
    assert publication_summary(mongo, saved)['complete']


def test_chat_plan_suggestion_approval_and_audit_persist_on_mongodb(mongo):
    from app.plan_suggestions import prepare_suggestion, record_suggestion, decide_suggestion, suggestion_state
    from app.training_plan import WeeklyPlan, save_plan_proposal
    from tests.test_training_plan import example_plan
    saved = save_plan_proposal(mongo, WeeklyPlan.model_validate(example_plan()), to_iso(START))
    proposal = prepare_suggestion(mongo, saved, [{"day": "Monday", "exercise": "Machine press", "weight_kg": 12.5, "reason": "A proposed trial progression."}],
                                  [{"id": "press", "title": "Machine press", "type": "weight_reps"}])
    record_suggestion(mongo, proposal, to_iso(START))
    assert mongo.latest_coach_reply()["status"] == "plan_draft"
    assert decide_suggestion(mongo, proposal["suggestion_id"], "apply", to_iso(END)) == 2
    for number in range(25): mongo.save_coach_reply(to_iso(END), 'answered', str(number), {'message': 'chat'})
    assert suggestion_state(mongo, proposal["suggestion_id"])[1] == "applied"
    assert len(mongo.plan_suggestion_events()) == 2
    assert mongo.latest_training_plan()["plan_version"] == 2


def test_garmin_daily_summaries_are_deduplicated_and_separate(mongo):
    from tests.test_garmin_data import storage_contract
    storage_contract(mongo)
    assert mongo.db.garmin_daily.count_documents({}) == 1
    assert mongo.db.workouts.count_documents({}) == 0
    assert mongo.db.check_ins.count_documents({}) == 0


def test_extended_garmin_values_are_visible_and_cleared_atomically_by_day(mongo):
    from tests.test_garmin_data import extended_storage_contract
    extended_storage_contract(mongo)
    assert mongo.db.garmin_daily.count_documents({}) == 1
    assert mongo.db.workouts.count_documents({}) == 0
    assert mongo.db.check_ins.count_documents({}) == 0


def test_garmin_snapshots_preserve_daily_data_and_state(mongo, tmp_path):
    from tests.test_garmin_data import storage_contract
    source=Database(str(tmp_path/'source.db'));source.migrate();storage_contract(source)
    snapshot=sqlite_snapshot(source.path)
    import_snapshot(snapshot,mongo)
    assert source.list_garmin_days()==mongo.list_garmin_days()
    assert source.get_garmin_state()==mongo.get_garmin_state()


def test_legacy_sqlite_without_garmin_tables_can_still_be_migrated(tmp_path):
    source=Database(str(tmp_path/'legacy.db'));source.migrate()
    with source.transaction() as conn:
        conn.execute('DROP TABLE garmin_daily');conn.execute('DROP TABLE garmin_state')
    snapshot=sqlite_snapshot(source.path)
    assert snapshot['garmin_daily']==[] and snapshot['garmin_state']==[]
