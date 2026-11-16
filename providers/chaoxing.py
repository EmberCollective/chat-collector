"""Opt-in SQL snapshot for explicitly authorized Chaoxing text captures.

Collection and credentials belong to integrations.chaoxing_provider.runtime.
This boundary opens only the capture database, never a browser or HTTP client.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from common.codec import digest
from providers.query_registry import register_query_provider


class ChaoxingSnapshot:
    def __init__(self, scope):
        self.db = None
        if scope['platform'] != 'chaoxing':
            raise ValueError('chaoxing_scope_mismatch')
        source = scope['config']['source']
        if str(scope['account_id']) != str(source['account_id']):
            raise ValueError('chaoxing_scope_mismatch')
        self.account = str(source['account_id'])
        ids = source.get('source_ids')
        if (not isinstance(ids, list) or not ids or
                any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids))):
            raise ValueError('explicit_unique_sources_required')
        path = Path(source['database']).resolve(strict=True)
        self.db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA query_only=ON')
            self.db.execute('BEGIN')
            account = self.db.execute("SELECT value FROM capture_metadata WHERE key='account_id'").fetchone()
            if not account or account[0] != self.account:
                raise ValueError('chaoxing_capture_account_mismatch')
            self.sources = {}
            self.rows = []
            for identifier in sorted(ids):
                row = self.db.execute('SELECT * FROM sources WHERE id=?', (identifier,)).fetchone()
                if not row or not row['enabled'] or row['since'] is None:
                    raise ValueError('source_missing_or_not_authorized')
                if row['kind'] not in {'chat', 'singlechat', 'groupchat'}:
                    raise ValueError('source_type_not_supported_yet')
                self.sources[identifier] = dict(row)
                self.rows.extend(dict(record) for record in self.db.execute('''
                    SELECT rowid AS ordinal,* FROM messages WHERE source_id=? AND baseline=0
                        AND sent_ms>=? AND captured>=? ORDER BY captured,id
                ''', (identifier, row['since'] * 1000, row['since']))
                    if record['body'] and not record['body'].startswith('【非文字'))
            self.snapshot_id = digest({'account': self.account, 'sources': self.sources, 'rows': self.rows})
        except Exception:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def scan(self, start, end, *, context=None, conversations=None):
        def room(row):
            kind = self.sources[row['source_id']]['kind']
            return ('group' if kind == 'groupchat' else 'direct', row['source_id'])
        rows = [row for row in self.rows if conversations is None or room(row) in conversations]
        selected = [row for row in rows if start <= row['captured'] < end]
        if context and context[0]:
            for conversation in sorted({room(row) for row in rows}):
                selected += sorted((row for row in rows if row['captured'] < start and room(row) == conversation),
                                   key=lambda row: (row['sent_ms'], row['ordinal']))[-context[0]:]
        for row in sorted(selected, key=lambda row: (row['sent_ms'], row['ordinal'])):
            yield dict(platform='chaoxing', account_id=self.account,
                       conversation_id=row['source_id'], conversation_type=room(row)[0],
                       message_id=row['id'], sender_id='', sender='名称未获取',
                       sent_at=int(row['sent_ms'] // 1000), text=row['body'], kind='text',
                       mentions_all=False, attachments=[], source={'local_id': row['ordinal'],
                       'provider': 'chaoxing-capture', 'captured_at': row['captured']},
                       extensions={'chaoxing': {'identity_basis': 'stable_local_capture_identity',
                                                'sender_mapping': 'unavailable'}})

    def enrich(self, message):
        return dict(message, identity={'account': {'id': self.account}, 'conversation': {
            'id': message['conversation_id'], 'display_name': None, 'missing_fields': ['verified_name']},
            'people': [], 'warnings': ['sender_identity_unresolved', 'conversation_name_unverified']})


@contextmanager
def snapshot(scope):
    value = ChaoxingSnapshot(scope)
    try:
        yield value
    finally:
        value.close()


def register():
    """Application composition calls this once; import has no registration side effects."""
    register_query_provider('chaoxing', snapshot)
