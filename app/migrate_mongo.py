"""Verified, resumable SQLite-to-MongoDB import. Never deletes the SQLite source."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pymongo.errors import PyMongoError

from app.config import Settings
from app.mongo import COLLECTIONS, MongoDatabase, STATE_FIELDS, date_value, public


class MigrationError(RuntimeError):
    pass


def sqlite_snapshot(path: str | Path) -> dict[str, list[dict]]:
    source = Path(path).resolve()
    if not source.is_file():
        raise MigrationError('SQLite source does not exist. No database was changed.')
    with sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        available = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        optional = {'garmin_daily', 'garmin_state'}
        required = (set(COLLECTIONS) - optional) | {'exercises', 'sets'}
        if not required.issubset(available):
            raise MigrationError('SQLite source schema is incomplete. Keep the source and check its path.')
        tables = {table: [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')] for table in required | (optional & available)}
        tables.update({table: [] for table in optional - available})
    nested_sets = {}
    for row in sorted(tables['sets'], key=lambda row: (row['workout_id'], row['exercise_index'], row['set_index'])):
        nested_sets.setdefault((row['workout_id'], row['exercise_index']), []).append(row)
    nested_exercises = {}
    for row in sorted(tables['exercises'], key=lambda row: (row['workout_id'], row['exercise_index'])):
        item = {**row, 'sets': nested_sets.get((row['workout_id'], row['exercise_index']), [])}
        nested_exercises.setdefault(row['workout_id'], []).append(item)
    result = {}
    for name in COLLECTIONS:
        id_key = 'date' if name in {'body_measurements', 'garmin_daily'} else 'workout_id' if name == 'hevy_webhook_jobs' else 'id'
        result[name] = []
        for row in tables[name]:
            if name in {'garmin_daily', 'garmin_state'}:
                body = json.loads(row['body_json'])
                document = {'_id': row[id_key], **body}
            else:
                document = {'_id': row[id_key], **row}
            if name == 'workouts':
                document.update(exercises=nested_exercises.get(row['id'], []), _revision=1, _start_at=date_value(row.get('start_time')))
            result[name].append(document)
        result[name].sort(key=lambda document: str(document['_id']))
    return result


def fingerprint(snapshot):
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=lambda value: value.isoformat(), separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def verify(snapshot: dict, target: MongoDatabase) -> dict:
    counts = {}
    for name, documents in snapshot.items():
        collection = target.db[name]
        if collection.count_documents({}) != len(documents):
            raise MigrationError(f'Verification failed: {name} count differs. SQLite was preserved.')
        for expected in documents:
            actual = collection.find_one({'_id': expected['_id']})
            if public(actual) != public(expected) or (name == 'workouts' and actual.get('_start_at') != expected.get('_start_at')):
                raise MigrationError(f'Verification failed: {name} contents differ. SQLite was preserved.')
        counts[name] = len(documents)
    return counts


def import_snapshot(snapshot: dict, target: MongoDatabase) -> dict:
    digest = fingerprint(snapshot)
    manifest = target.db.migrations.find_one({'_id': 'sqlite-import'})
    if manifest:
        if manifest.get('fingerprint') != digest:
            raise MigrationError('Target belongs to a different SQLite snapshot. Refusing to overwrite MongoDB.')
        if manifest.get('status') == 'verified':
            return verify(snapshot, target)
        if manifest.get('status') != 'copying':
            raise MigrationError('Migration state is not importable. No records were replaced.')
    else:
        for name in target.db.list_collection_names():
            count = target.db[name].count_documents({})
            if name == 'sync_state' and count == 1:
                state = target.db.sync_state.find_one({'_id': 1})
                if state and all(state.get(field) is None for field in STATE_FIELDS):
                    continue
            if count:
                raise MigrationError('MongoDB target contains existing data. Refusing to overwrite it.')
        target.db.migrations.insert_one({'_id': 'sqlite-import', 'fingerprint': digest, 'status': 'copying'})
    for name, documents in snapshot.items():
        for document in documents:
            target.db[name].replace_one({'_id': document['_id']}, document, upsert=True)
    for name in ('sync_runs', 'goals', 'check_ins', 'coach_replies'):
        maximum = max((document['id'] for document in snapshot[name]), default=0)
        target.db.counters.update_one({'_id': name}, {'$max': {'value': maximum}}, upsert=True)
    counts = verify(snapshot, target)
    target.db.migrations.update_one({'_id': 'sqlite-import'}, {'$set': {
        'status': 'verified', 'verified_at': datetime.now(timezone.utc), 'counts': counts,
    }})
    return counts


def backup_sqlite(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise MigrationError('Backup path already exists. Choose a new backup filename.')
    # The backup API includes committed WAL data. A file copy would not guarantee that.
    destination.touch(mode=0o600, exist_ok=False)
    with sqlite3.connect(f'{source.resolve().as_uri()}?mode=ro', uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    destination.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='./data/fitness.db')
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--backup', help='New local SQLite backup path; never overwrites an existing file.')
    args = parser.parse_args()
    settings = Settings()
    target = None
    try:
        source = Path(args.source)
        if not source.is_file():
            raise MigrationError('SQLite source does not exist. No database was changed.')
        if args.verify_only:
            snapshot = sqlite_snapshot(source)
        else:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = Path(args.backup) if args.backup else source.parent / 'backups' / f'pre-mongodb-{stamp}.db'
            backup_sqlite(source, backup)
            snapshot = sqlite_snapshot(backup)
        target = MongoDatabase(settings.mongodb_uri, settings.mongodb_database,
            username=settings.mongodb_username, password=settings.mongodb_password,
            auth_source=settings.mongodb_auth_source)
        if args.verify_only:
            counts = verify(snapshot, target)
        else:
            target.migrate()
            counts = import_snapshot(snapshot, target)
        print(json.dumps({'status': 'verified', 'counts': counts, 'sqlite_source_preserved': True}))
    except (MigrationError, PyMongoError, sqlite3.Error, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, MigrationError) else 'Database migration could not finish. SQLite was preserved; check local database access.'
        print(message)
        raise SystemExit(1) from None
    finally:
        if target:
            target.close()


if __name__ == '__main__':
    main()
