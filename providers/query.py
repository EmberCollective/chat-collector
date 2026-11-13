"""Provider-owned standard SQL environment and read-only rule execution."""
import json
from pathlib import Path
import sqlite3
import tempfile

from common.codec import digest
from .query_registry import query_snapshot


def _schema(db):
    db.executescript('''
        PRAGMA foreign_keys=ON;
        CREATE TABLE messages(
            identity TEXT PRIMARY KEY, platform TEXT, account_id TEXT,
            conversation_id TEXT, conversation_type TEXT, sender_id TEXT,
            sent_at INTEGER, message_id TEXT, local_id INTEGER, database_name TEXT,
            text TEXT, kind TEXT, mentions_all INTEGER, eligible INTEGER, body TEXT);
        CREATE INDEX conversation_time ON messages(
            platform,account_id,conversation_type,conversation_id,sent_at,local_id);
        CREATE VIEW candidates AS SELECT * FROM messages WHERE eligible=1;
        CREATE TABLE hits(identity TEXT NOT NULL REFERENCES messages(identity),
            rule TEXT NOT NULL, PRIMARY KEY(identity,rule));
    ''')


def _read_only(action, first, second, database, trigger):
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if first in {'messages', 'candidates'} else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_DENY if second == 'load_extension' or second.startswith('pragma_') else sqlite3.SQLITE_OK
    return sqlite3.SQLITE_OK if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE} else sqlite3.SQLITE_DENY


def validate_rule(sql, params, recent_count, file):
    """Validate SQL in the same compatibility schema used for real execution."""
    db = sqlite3.connect(':memory:')
    try:
        _schema(db)
        db.set_authorizer(_read_only)
        db.execute('EXPLAIN ' + sql, dict(params, window_start=0, window_end=1,
                                        recent_count=recent_count)).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f'Invalid read-only SQL ruleset {Path(file).name}: {exc}') from exc
    finally:
        db.close()


def query_rules(scope, window, rules):
    """Return selected standard rows and every rule hit from one verified snapshot."""
    start, end = window['start_inclusive'], window['end_exclusive']
    if (type(start) is not int or type(end) is not int or end <= start):
        raise ValueError('end must be after start; window boundaries must be Unix seconds')
    if any(not isinstance(rule.get('id'), str) or not rule['id'] for rule in rules):
        raise ValueError('Each query rule requires a stable id')
    with tempfile.TemporaryDirectory(prefix='provider-query-') as folder:
        db = sqlite3.connect(Path(folder) / 'round.db')
        try:
            _schema(db)
            with query_snapshot(scope) as snapshot:
                conversations = set()

                def insert(message, eligible):
                    if (message.get('platform') != scope['platform'] or
                            str(message.get('account_id')) != str(scope['account_id'])):
                        raise ValueError('Provider message does not match query scope')
                    source = message.get('source', {})
                    identity = json.dumps([message.get(k) for k in (
                        'platform', 'account_id', 'conversation_type', 'conversation_id', 'message_id')])
                    db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                        identity, message['platform'], message['account_id'], message['conversation_id'],
                        message.get('conversation_type', 'direct'), message['sender_id'], message['sent_at'],
                        message['message_id'], int(source.get('local_id', 0)), Path(source.get('database', '')).name,
                        message['text'], message['kind'], int(message['mentions_all']), eligible,
                        json.dumps(message, ensure_ascii=False)))

                for message in snapshot.scan(start, end):
                    insert(message, 1)
                    conversations.add((message.get('conversation_type', 'direct'), message['conversation_id']))
                count = max((rule['recent_count'] for rule in rules), default=0)
                if count and conversations:
                    # An anchor N rows before the window needs another N preceding rows.
                    for message in snapshot.scan(start, start, context=(2 * count, 0), conversations=conversations):
                        insert(message, 0)
                db.commit()
                for rule in rules:
                    bound = dict(rule['params'], window_start=start, window_end=end,
                                 recent_count=rule['recent_count'])
                    try:
                        db.set_authorizer(_read_only)
                        cursor = db.execute(rule['sql'], bound)
                        if [column[0] for column in cursor.description or []] != ['identity']:
                            raise ValueError('SQL must return exactly one column named identity')
                        rows = cursor.fetchall()
                        if any(not isinstance(row[0], str) for row in rows):
                            raise ValueError('SQL identity must be a non-null string from messages')
                    except (sqlite3.Error, ValueError) as exc:
                        raise ValueError(f'Ruleset {Path(rule["file"]).name} failed: {exc}') from exc
                    finally:
                        db.set_authorizer(None)
                    try:
                        db.executemany('INSERT OR IGNORE INTO hits VALUES(?,?)',
                                       ((row[0], rule['id']) for row in rows))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError(f'Ruleset {Path(rule["file"]).name} returned an unknown identity') from exc
                cursor = db.execute('''WITH ordered AS (
                        SELECT *,lag(body) OVER (
                            PARTITION BY platform,account_id,conversation_type,conversation_id
                            ORDER BY sent_at,local_id,database_name,message_id,identity) AS previous_body
                        FROM messages
                    ) SELECT identity,body,previous_body FROM ordered
                        WHERE identity IN (SELECT identity FROM hits)
                        ORDER BY sent_at,local_id,database_name,message_id,identity''')
                messages = []
                for identity, body, previous_body in cursor:
                    message = json.loads(body)
                    previous = json.loads(previous_body) if previous_body else None
                    message.setdefault('source', {})['selection_context'] = {
                        'previous_message_key': _message_key(previous) if previous else None,
                        'previous_known': previous is not None,
                    }
                    messages.append(dict(identity=identity, message_key=_message_key(message),
                                         message=snapshot.enrich(message)))
                result = dict(snapshot_id=snapshot.snapshot_id, messages=messages,
                              hits=[dict(identity=identity, rule_id=rule_id) for identity, rule_id in
                                    db.execute('SELECT identity,rule FROM hits ORDER BY identity,rule')])
            return result
        finally:
            db.close()


def _message_key(message):
    return digest([str(message[k]) for k in ('platform', 'account_id', 'conversation_id', 'message_id')])


