"""MongoDB repository. A workout and all its sets change as one atomic document."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.write_concern import WriteConcern

from app.db import _as_int, _is_newer
from app.timeutil import parse_iso

MEASUREMENT_FIELDS = (
    'weight_kg', 'lean_mass_kg', 'fat_percent', 'neck_cm', 'shoulder_cm', 'chest_cm',
    'left_bicep_cm', 'right_bicep_cm', 'left_forearm_cm', 'right_forearm_cm',
    'abdomen', 'waist', 'hips', 'left_thigh', 'right_thigh', 'left_calf', 'right_calf',
)
STATE_FIELDS = (
    'events_since', 'last_attempt_at', 'last_success_at', 'last_error', 'last_warning',
    'last_mode', 'remote_workout_count', 'local_workout_count',
)
COLLECTIONS = (
    'workouts', 'sync_state', 'sync_runs', 'profile', 'hevy_webhook_jobs', 'goals',
    'check_ins', 'body_measurements', 'body_entry', 'coach_replies', 'garmin_daily', 'garmin_state',
)


def public(document: dict | None) -> dict | None:
    if document is None:
        return None
    return {key: deepcopy(value) for key, value in document.items() if not key.startswith('_')}


def date_value(value: str | None) -> datetime | None:
    try:
        return parse_iso(value)
    except (ValueError, TypeError):
        return None


def workout_document(workout: dict, synced_at: str) -> dict:
    wid = str(workout['id'])
    result = {
        '_id': wid, 'id': wid, 'title': workout.get('title'),
        'routine_id': workout.get('routine_id'), 'description': workout.get('description'),
        'start_time': workout.get('start_time'), 'end_time': workout.get('end_time'),
        'hevy_created_at': workout.get('created_at'), 'hevy_updated_at': workout.get('updated_at'),
        'source_time': workout.get('updated_at'), 'deleted_at': None,
        'raw_json': json.dumps(workout, ensure_ascii=False, sort_keys=True),
        'local_synced_at': synced_at, '_start_at': date_value(workout.get('start_time')),
        'exercises': [],
    }
    seen_exercises = set()
    for pos, exercise in enumerate(workout.get('exercises') or []):
        index = _as_int(exercise.get('index'), pos)
        if index in seen_exercises:
            raise ValueError('Duplicate exercise index in saved workout.')
        seen_exercises.add(index)
        item = {
            'workout_id': wid, 'exercise_index': index, 'title': exercise.get('title'),
            'notes': exercise.get('notes'), 'exercise_template_id': exercise.get('exercise_template_id'),
            'superset_id': exercise.get('superset_id'), 'sets': [],
        }
        seen_sets = set()
        for set_pos, entry in enumerate(exercise.get('sets') or []):
            set_index = _as_int(entry.get('index'), set_pos)
            if set_index in seen_sets:
                raise ValueError('Duplicate set index in saved workout.')
            seen_sets.add(set_index)
            item['sets'].append({
                'workout_id': wid, 'exercise_index': index, 'set_index': set_index,
                'set_type': entry.get('type'), **{key: entry.get(key) for key in (
                    'weight_kg', 'reps', 'distance_meters', 'duration_seconds', 'rpe', 'custom_metric',
                )},
            })
        item['sets'].sort(key=lambda value: value['set_index'])
        result['exercises'].append(item)
    result['exercises'].sort(key=lambda value: value['exercise_index'])
    return result


class MongoDatabase:
    def __init__(self, uri: str, database: str, *, username: str = '', password: str = '', auth_source: str = 'admin'):
        options = {'username': username, 'password': password, 'authSource': auth_source} if username else {}
        self.client = MongoClient(
            uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
            socketTimeoutMS=15000, appname='AmoreGymBro', tz_aware=True, **options,
        )
        self.db = self.client.get_database(database, write_concern=WriteConcern(w=1, j=True))
        # This identifier is safe to display; it never includes a connection credential.
        self.path = f'mongodb:{database}'

    def migrate(self):
        try:
            self.client.admin.command('ping')
            self.db.workouts.create_index([('deleted_at', ASCENDING), ('_start_at', DESCENDING)])
            self.db.body_measurements.create_index('date', unique=True)
            self.db.garmin_daily.create_index('date', unique=True)
            self.db.hevy_webhook_jobs.create_index([('status', ASCENDING), ('next_attempt_at', ASCENDING)])
            self.db.goals.create_index([('created_at', DESCENDING), ('id', DESCENDING)])
            self.db.check_ins.create_index([('created_at', DESCENDING), ('id', DESCENDING)])
            self.db.sync_state.update_one({'_id': 1}, {'$setOnInsert': {'id': 1, **dict.fromkeys(STATE_FIELDS)}}, upsert=True)
        except PyMongoError:
            raise RuntimeError('MongoDB is unavailable or authentication failed. Check the local database settings.') from None

    def close(self):
        self.client.close()

    def _next_id(self, name):
        counter = self.db.counters.find_one_and_update(
            {'_id': name}, {'$inc': {'value': 1}}, upsert=True, return_document=ReturnDocument.AFTER,
        )
        return int(counter['value'])

    def _active_filter(self, start=None, end=None):
        query = {'deleted_at': None}
        if start is not None or end is not None:
            query['_start_at'] = {}
            if start is not None:
                query['_start_at']['$gte'] = parse_iso(start)
            if end is not None:
                query['_start_at']['$lt'] = parse_iso(end)
        return query

    def get_sync_state(self):
        return public(self.db.sync_state.find_one({'_id': 1})) or {}

    def count_active(self, start=None, end=None):
        return self.db.workouts.count_documents(self._active_filter(start, end))

    def list_workouts(self, limit, offset):
        cursor = self.db.workouts.find(self._active_filter()).sort([('_start_at', DESCENDING), ('id', DESCENDING)]).skip(max(0, offset)).limit(max(1, limit))
        result = []
        for document in cursor:
            row = public(document)
            exercises = row.pop('exercises', [])
            row['set_count'] = sum(len(exercise.get('sets', [])) for exercise in exercises)
            result.append(row)
        return result

    def get_workout(self, workout_id):
        return public(self.db.workouts.find_one({'_id': workout_id}))

    def _versioned_write(self, wid, incoming_time, build):
        for _ in range(20):
            existing = self.db.workouts.find_one({'_id': wid})
            if existing is not None and not _is_newer(incoming_time, date_value(existing.get('source_time'))):
                return 'ignored'
            document = build(existing)
            document['_revision'] = (existing or {}).get('_revision', 0) + 1
            if existing is None:
                try:
                    self.db.workouts.insert_one(document)
                    return 'inserted'
                except DuplicateKeyError:
                    continue
            result = self.db.workouts.replace_one({'_id': wid, '_revision': existing.get('_revision')}, document)
            if result.matched_count:
                return 'updated'
        raise RuntimeError('Concurrent workout updates; retry synchronization.')

    def apply_workout(self, workout, synced_at):
        if not workout.get('id'):
            return 'ignored'
        document = workout_document(workout, synced_at)
        return self._versioned_write(document['_id'], date_value(workout.get('updated_at')), lambda existing: document)

    def mark_deleted(self, workout_id, deleted_at, synced_at):
        event_time = deleted_at or synced_at
        def tombstone(existing):
            document = existing or {
                '_id': workout_id, 'id': workout_id,
                **dict.fromkeys(('title', 'routine_id', 'description', 'start_time', 'end_time', 'hevy_created_at', 'hevy_updated_at')),
                'exercises': [], '_start_at': None,
                'raw_json': json.dumps({'id': workout_id, 'deleted_at': deleted_at, 'type': 'deleted'}, sort_keys=True),
            }
            return {**document, 'deleted_at': event_time, 'source_time': event_time, 'local_synced_at': synced_at}
        outcome = self._versioned_write(str(workout_id), date_value(event_time), tombstone)
        return 'ignored' if outcome == 'ignored' else 'deleted'

    def save_profile(self, user, synced_at):
        self.db.profile.replace_one({'_id': 1}, {
            '_id': 1, 'id': 1, 'hevy_user_id': user.get('id'), 'username': user.get('username'),
            'display_name': user.get('name'),
            'weight_unit': user.get('weight_unit') if user.get('weight_unit') in {'kg', 'lbs'} else None,
            'distance_unit': user.get('distance_unit') if user.get('distance_unit') in {'kilometers', 'miles'} else None,
            'synced_at': synced_at,
        }, upsert=True)

    def get_profile(self):
        return public(self.db.profile.find_one({'_id': 1}))

    def start_run(self, started_at, mode):
        run_id = self._next_id('sync_runs')
        self.db.sync_state.update_one({'_id': 1}, {'$set': {'last_attempt_at': started_at, 'last_mode': mode, 'last_error': None}})
        self.db.sync_runs.insert_one({
            '_id': run_id, 'id': run_id, 'started_at': started_at, 'finished_at': None,
            'mode': mode, 'status': 'running', 'workouts_upserted': 0,
            'workouts_deleted': 0, 'workouts_ignored': 0, 'error': None,
        })
        return run_id

    def finish_run(self, run_id, *, finished_at, status, upserted, deleted, ignored, error, warning, events_since, remote_count, local_count, advance_cursor):
        # Persist the run first. A failure before cursor commit only replays safe, deduplicated events.
        self.db.sync_runs.update_one({'_id': run_id}, {'$set': {
            'finished_at': finished_at, 'status': status, 'workouts_upserted': upserted,
            'workouts_deleted': deleted, 'workouts_ignored': ignored, 'error': error,
        }})
        state = {'last_warning': warning, 'local_workout_count': local_count}
        if advance_cursor:
            state.update(last_success_at=finished_at, last_error=None, events_since=events_since, remote_workout_count=remote_count)
        else:
            state['last_error'] = error
        self.db.sync_state.update_one({'_id': 1}, {'$set': state})

    def recent_runs(self, limit=5):
        return [public(row) for row in self.db.sync_runs.find().sort('_id', DESCENDING).limit(max(1, limit))]

    def enqueue_webhook_job(self, workout_id, received_at):
        try:
            self.db.hevy_webhook_jobs.insert_one({
                '_id': workout_id, 'workout_id': workout_id, 'status': 'pending', 'attempts': 0,
                'received_at': received_at, 'updated_at': received_at, 'next_attempt_at': None, 'last_error': None,
            })
            return 'queued'
        except DuplicateKeyError:
            result = self.db.hevy_webhook_jobs.update_one({'_id': workout_id, 'status': 'failed'}, {'$set': {
                'status': 'pending', 'attempts': 0, 'updated_at': received_at, 'next_attempt_at': None, 'last_error': None,
            }})
            return 'requeued' if result.modified_count else 'duplicate'

    def get_webhook_job(self, workout_id):
        return public(self.db.hevy_webhook_jobs.find_one({'_id': workout_id}))

    def webhook_counts(self):
        counts = {status: 0 for status in ('pending', 'running', 'retry', 'done', 'failed')}
        for row in self.db.hevy_webhook_jobs.aggregate([{'$group': {'_id': '$status', 'n': {'$sum': 1}}}]):
            counts[row['_id']] = row['n']
        return counts

    def claim_due_webhook_jobs(self, now, limit=5):
        result = []
        for _ in range(max(0, limit)):
            row = self.db.hevy_webhook_jobs.find_one_and_update(
                {'status': {'$in': ['pending', 'retry']}, '$or': [{'next_attempt_at': None}, {'next_attempt_at': {'$lte': now}}]},
                {'$set': {'status': 'running', 'updated_at': now}, '$inc': {'attempts': 1}},
                sort=[('received_at', ASCENDING), ('workout_id', ASCENDING)],
            )
            if row is None:
                break
            result.append(row['workout_id'])
        return result

    def complete_webhook_job(self, workout_id, *, status, now, error, next_attempt_at):
        self.db.hevy_webhook_jobs.update_one({'_id': workout_id}, {'$set': {
            'status': status, 'updated_at': now, 'last_error': error, 'next_attempt_at': next_attempt_at,
        }})

    def recover_running_webhook_jobs(self, now):
        return self.db.hevy_webhook_jobs.update_many({'status': 'running'}, {'$set': {
            'status': 'pending', 'updated_at': now, 'next_attempt_at': None,
        }}).modified_count

    def upsert_body_measurement(self, measurement, synced_at):
        day = str(measurement.get('date') or '').strip()
        if len(day) != 10:
            return 'ignored'
        self.db.body_measurements.replace_one({'_id': day}, {
            '_id': day, 'date': day, **{key: measurement.get(key) for key in MEASUREMENT_FIELDS},
            'raw_json': json.dumps(measurement), 'local_synced_at': synced_at,
        }, upsert=True)
        return 'upserted'

    def body_weight_history(self, limit=30):
        rows = list(self.db.body_measurements.find({'weight_kg': {'$gt': 0}}, {'date': 1, 'weight_kg': 1}).sort('date', DESCENDING).limit(max(1, min(limit, 365))))
        return [public(row) for row in reversed(rows)]

    def latest_body_measurement(self):
        return public(self.db.body_measurements.find_one(sort=[('date', DESCENDING)]))

    def get_body_entry(self):
        return public(self.db.body_entry.find_one({'_id': 1}))

    def save_body_entry(self, *, height_cm, age_years, sex, activity, updated_at, days_per_week=None, session_minutes=None, train_where=None):
        self.db.body_entry.replace_one({'_id': 1}, {
            '_id': 1, 'id': 1, 'height_cm': height_cm, 'age_years': age_years, 'sex': sex,
            'activity': activity, 'updated_at': updated_at, 'days_per_week': days_per_week,
            'session_minutes': session_minutes, 'train_where': train_where,
        }, upsert=True)

    def save_goal(self, statement, created_at):
        goal_id = self._next_id('goals')
        self.db.goals.insert_one({'_id': goal_id, 'id': goal_id, 'statement': statement, 'created_at': created_at})
        return goal_id

    def list_goals(self):
        return [public(row) for row in self.db.goals.find().sort([('created_at', DESCENDING), ('id', DESCENDING)])]

    def delete_goal(self, goal_id):
        return bool(self.db.goals.delete_one({'_id': goal_id}).deleted_count)

    def save_check_in(self, feeling, notes, created_at):
        check_id = self._next_id('check_ins')
        self.db.check_ins.insert_one({'_id': check_id, 'id': check_id, 'feeling': feeling, 'notes': notes, 'created_at': created_at})
        return check_id

    def list_check_ins(self, limit=30):
        return [public(row) for row in self.db.check_ins.find().sort([('created_at', DESCENDING), ('id', DESCENDING)]).limit(max(1, limit))]

    def delete_check_in(self, check_in_id):
        return bool(self.db.check_ins.delete_one({'_id': check_in_id}).deleted_count)

    def active_workout_ids(self):
        return {row['id'] for row in self.db.workouts.find(self._active_filter(), {'id': 1})}

    def confirmed_exercise_rows(self, start=None, end=None):
        rows = []
        for document in self.db.workouts.find(self._active_filter(start, end)).sort([('start_time', ASCENDING), ('id', ASCENDING)]):
            for exercise in document.get('exercises', []):
                for item in exercise.get('sets') or [None]:
                    row = {
                        'workout_id': document['id'], 'workout_title': document.get('title'), 'start_time': document.get('start_time'),
                        'exercise_index': exercise['exercise_index'], 'exercise_title': exercise.get('title'),
                        'exercise_notes': exercise.get('notes'), 'exercise_template_id': exercise.get('exercise_template_id'),
                    }
                    row.update({key: (item or {}).get(key) for key in (
                        'set_index', 'set_type', 'weight_kg', 'reps', 'distance_meters', 'duration_seconds', 'rpe', 'custom_metric',
                    )})
                    rows.append(row)
        return rows

    def save_coach_reply(self, created_at, status, question, body):
        reply_id = self._next_id('coach_replies')
        self.db.coach_replies.insert_one({
            '_id': reply_id, 'id': reply_id, 'created_at': created_at, 'status': status,
            'question': question, 'body_json': json.dumps(body, ensure_ascii=False),
        })
        ordinary = {'status': {'$nin': ['plan_draft', 'hevy_publication', 'plan_suggestion']}}
        kept = list(self.db.coach_replies.find(ordinary, {'_id': 1}).sort('_id', DESCENDING).limit(20))
        if len(kept) == 20:
            self.db.coach_replies.delete_many({'_id': {'$lt': kept[-1]['_id']}, **ordinary})

    def latest_coach_reply(self):
        row = self.db.coach_replies.find_one({'status': {'$nin': ['hevy_publication', 'plan_suggestion']}}, sort=[('_id', DESCENDING)])
        if row is None:
            return None
        return {**json.loads(row['body_json']), 'question': row['question'], 'status': row['status'], 'created_at': row['created_at'], 'reply_id': row['id']}

    def latest_training_plan(self):
        row = self.db.coach_replies.find_one({'status': 'plan_draft'}, sort=[('_id', DESCENDING)])
        if row is None:
            return None
        return {**json.loads(row['body_json']), 'created_at': row['created_at'], 'status': row['status']}

    def publication_events(self):
        return [json.loads(row['body_json']) for row in self.db.coach_replies.find({'status': 'hevy_publication'}).sort('_id', ASCENDING)]

    def plan_suggestion_events(self):
        return [json.loads(row['body_json']) for row in self.db.coach_replies.find({'status': 'plan_suggestion'}).sort('_id', ASCENDING)]

    def get_garmin_state(self):
        return public(self.db.garmin_state.find_one({'_id': 1})) or {}

    def save_garmin_state(self, state):
        self.db.garmin_state.replace_one({'_id': 1}, {'_id': 1, **state}, upsert=True)

    def upsert_garmin_days(self, rows, synced_at):
        for row in rows:
            self.db.garmin_daily.replace_one({'_id': row['date']}, {'_id': row['date'], **row, 'synced_at': synced_at}, upsert=True)

    def list_garmin_days(self, limit=7):
        from app.garmin_data import SCALAR_FIELDS
        present = [{key: {'$ne': None}} for key in SCALAR_FIELDS]
        present.append({'sleep_scores': {'$exists': True, '$nin': [None, {}]}})
        return [public(row) for row in self.db.garmin_daily.find({'$or': present}).sort('date', DESCENDING).limit(min(31, max(1, limit)))]
