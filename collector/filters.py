"""One round snapshot, registered SQL queries, and unique selected message IDs."""
import importlib
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from .store import canonical, digest
from .identities import IdentityContext


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


def load_rulesets(config):
    from .config import validate_filters
    settings = config.get('filters', {})
    validate_filters(settings)
    entries = settings.get('rulesets', [])
    if not isinstance(entries, list):
        raise ValueError('filters.rulesets must be an array')
    base = Path(config.get('_config_path', '.')).resolve().parent
    result = []
    db = sqlite3.connect(':memory:')
    try:
        _schema(db)
        db.set_authorizer(_read_only)
        for entry in entries:
            if not isinstance(entry, dict) or set(entry)-{'file', 'params', 'recent_count', 'enabled'} or not entry.get('file'):
                raise ValueError('Each ruleset needs file, optional enabled, params and recent_count')
            if not isinstance(entry['file'], str) or Path(entry['file']).suffix.lower() != '.sql':
                raise ValueError('ruleset.file must name a .sql file')
            if not isinstance(entry.get('enabled', True), bool):
                raise ValueError('ruleset.enabled must be true or false')
            count = entry.get('recent_count', 0)
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1000:
                raise ValueError('ruleset.recent_count must be an integer from 0 to 1000')
            params = entry.get('params', {})
            if not isinstance(params, dict) or any(
                not isinstance(k, str) or k.startswith('_') or k in {'window_start', 'window_end', 'recent_count'}
                or (v is not None and not isinstance(v, (str, int, float))) for k, v in params.items()
            ):
                raise ValueError('Ruleset params must be named scalars; window_start/window_end/recent_count and _ prefixes are reserved')
            if not entry.get('enabled', True):
                continue
            path = (base / entry['file']).resolve()
            sql = path.read_text(encoding='utf-8-sig').strip()
            bound = dict(params, window_start=0, window_end=1, recent_count=count)
            try:
                db.execute('EXPLAIN '+sql, bound).fetchall()
            except sqlite3.Error as exc:
                raise ValueError(f'Invalid read-only SQL ruleset {path.name}: {exc}') from exc
            provenance = dict(file=Path(entry['file']).as_posix(), params=params,
                              recent_count=count, sql_sha256=hashlib.sha256(sql.encode('utf-8')).hexdigest())
            result.append(dict(file=str(path), sql=sql, params=params, recent_count=count,
                               provenance=provenance))
    finally:
        db.close()
    return result


def scan_filtered(config, start, end, *, rulesets=None):
    """Finish all rules before yielding; SQL explicitly chooses candidates or context."""
    if end <= start:
        raise ValueError('end must be after start')
    if rulesets is None:
        rulesets = load_rulesets(config)
    if not rulesets:
        return
    adapter = importlib.import_module('collector.adapters.'+config['platform'])
    count = max((r['recent_count'] for r in rulesets), default=0)
    with tempfile.TemporaryDirectory(prefix='collector-filter-') as folder:
        db = sqlite3.connect(Path(folder)/'round.db')
        try:
            _schema(db)
            conversations = set()

            def insert(message, eligible):
                source = message.get('source', {})
                identity = json.dumps([message.get(k) for k in (
                    'platform', 'account_id', 'conversation_type', 'conversation_id', 'message_id')])
                db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                    identity, message['platform'], message['account_id'], message['conversation_id'],
                    message.get('conversation_type', 'direct'), message['sender_id'], message['sent_at'],
                    message['message_id'], int(source.get('local_id', 0)), Path(source.get('database', '')).name,
                    message['text'], message['kind'], int(message['mentions_all']), eligible,
                    json.dumps(message, ensure_ascii=False)))

            for message in adapter.scan(config['source'], start, end):
                insert(message, 1)
                conversations.add((message['conversation_type'], message['conversation_id']))
            if count and conversations:
                # An anchor N rows before the window can need another N rows of context.
                for message in adapter.scan(config['source'], start, start, context=(2 * count, 0), conversations=conversations):
                    insert(message, 0)
            db.commit()
            for rule in rulesets:
                bound = dict(rule['params'], window_start=start, window_end=end, recent_count=rule['recent_count'])
                cursor = db.cursor()
                try:
                    db.set_authorizer(_read_only)
                    cursor.execute(rule['sql'], bound)
                    if [column[0] for column in cursor.description or []] != ['identity']:
                        raise ValueError('SQL must return exactly one column named identity')
                    rows = cursor.fetchall()
                    if any(not isinstance(row[0], str) for row in rows):
                        raise ValueError('SQL identity must be a non-null string from messages')
                except (sqlite3.Error, ValueError) as exc:
                    raise ValueError(f'Ruleset {Path(rule["file"]).name} failed: {exc}') from exc
                finally:
                    cursor.close()
                    db.set_authorizer(None)
                # Rules share all input rows; only their output IDs are deduplicated.
                try:
                    db.executemany('INSERT OR IGNORE INTO hits VALUES(?,?)',
                                   ((row[0], canonical(rule['provenance'])) for row in rows))
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f'Ruleset {Path(rule["file"]).name} returned an unknown identity') from exc
            cursor = db.execute('''WITH ordered AS (
                    SELECT *,lag(body) OVER (
                        PARTITION BY platform,account_id,conversation_type,conversation_id
                        ORDER BY sent_at,local_id,database_name,message_id,identity) AS previous_body
                    FROM messages
                ), matches AS (
                    SELECT identity,json_group_array(json(rule)) AS rules
                    FROM (SELECT identity,rule FROM hits ORDER BY identity,rule) GROUP BY identity
                ) SELECT body,rules,previous_body FROM ordered JOIN matches USING(identity)
                    ORDER BY sent_at,local_id,database_name,message_id,identity''')
            identities = IdentityContext(config)
            try:
                for body, rules, previous_body in cursor:
                    message = json.loads(body)
                    previous = json.loads(previous_body) if previous_body else None
                    message.setdefault('source', {})['selection_context'] = {
                        'previous_message_key': digest([str(previous[k]) for k in (
                            'platform', 'account_id', 'conversation_id', 'message_id')]) if previous else None,
                        'previous_known': previous is not None,
                    }
                    yield identities.enrich(dict(message, matched_rules=json.loads(rules)))
            finally:
                identities.close()
                cursor.close()
        finally:
            db.close()
