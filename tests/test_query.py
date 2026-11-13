from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

import pytest

from common.codec import digest
from collector.filters import load_rulesets
from providers.query import query_rules
from providers.query_registry import QUERY_PROVIDERS, register_query_provider
from test_filters import config, register


def query(config, start=100, end=111):
    rules = load_rulesets(config)
    for rule in rules:
        rule['id'] = digest(rule['provenance'])
    return query_rules(dict(provider_id=config['platform'], platform=config['platform'],
                            account_id=config['source']['account_id'], config=config),
                       dict(start_inclusive=start, end_exclusive=end), rules)


def test_native_schemas_share_query_contract_trigger_and_all_hits(config):
    register(config, 'files.sql')
    register(config, 'context.sql', 5)
    register(config, 'keyword.sql', keyword='document')
    result = query(config)
    assert len(result['snapshot_id']) == 64
    assert [int(row['message']['source']['local_id']) for row in result['messages']] == [1,2,3,4,5,6]
    file = next(row for row in result['messages'] if row['message']['kind'] == 'file')
    assert file['message']['attachments'][0]['name'] == 'document'
    assert len([hit for hit in result['hits'] if hit['identity'] == file['identity']]) == 3
    assert len(file['message_key']) == 64
    assert file['message']['source']['selection_context']['previous_known']
    assert 'matched_rules' not in file['message']
    assert result == query(config)


class SyntheticSnapshot:
    """A third source schema; SQL adaptation stays in the query boundary."""
    snapshot_id = 'synthetic-v1'

    def __init__(self, path):
        self.db = sqlite3.connect(path)

    def scan(self, start, end, *, context=None, conversations=None):
        rows = self.db.execute('SELECT ordinal,epoch,room,payload,document FROM entries ORDER BY epoch,ordinal').fetchall()
        if conversations is not None:
            rows = [row for row in rows if ('group', row[2]) in conversations]
        selected = [row for row in rows if start <= row[1] < end]
        if context:
            for room in sorted({row[2] for row in rows}):
                selected += [row for row in rows if row[1] < start and row[2] == room][-context[0]:]
        for ordinal, epoch, room, payload, document in selected:
            yield dict(platform='synthetic', account_id='account', conversation_type='group',
                       conversation_id=room, message_id=str(ordinal), sender_id='sender',
                       sent_at=epoch, text=payload, kind='file' if document else 'text',
                       mentions_all=False, attachments=[{'type':'file','name':'document'}] if document else [],
                       source={'local_id':ordinal,'database':'third.db'})

    def enrich(self, message):
        return dict(message, identity={'account':{'id':'account'}, 'conversation':{
            'id':message['conversation_id']}, 'people':[{'id':'sender'}]})


def test_third_provider_runs_unmodified_sql_and_arbitrary_bound_parameters(tmp_path, monkeypatch):
    path = tmp_path / 'third.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE entries(ordinal,epoch,room,payload,document)')
        db.executemany('INSERT INTO entries VALUES(?,?,?,?,?)', [
            (1,90,'a','prior',0), (2,100,'a','document',1), (3,101,'a','tail',0),
            (4,101,'other','unrelated',0)])
    @contextmanager
    def factory(scope):
        snapshot = SyntheticSnapshot(scope['config']['source']['database'])
        try:
            yield snapshot
        finally:
            snapshot.db.close()
    monkeypatch.setattr('providers.query_registry.QUERY_PROVIDERS', dict(QUERY_PROVIDERS))
    register_query_provider('synthetic', factory)
    cfg = dict(platform='synthetic',source={'account_id':'account','database':str(path)}, filters={})
    register(cfg,'context.sql',5)
    register(cfg,'files.sql')
    custom = tmp_path / 'custom.sql'
    custom.write_text('SELECT identity FROM candidates WHERE text=:another_parameter AND :a_number=42')
    cfg['filters']['rulesets'].append(dict(file=str(custom),params={'another_parameter':'document','a_number':42}))
    before = path.read_bytes()
    result = query(cfg)
    assert result['snapshot_id'] == 'synthetic-v1'
    assert [row['message']['message_id'] for row in result['messages']] == ['1','2','3']
    identity = result['messages'][1]['identity']
    assert len([hit for hit in result['hits'] if hit['identity']==identity]) == 3
    assert path.read_bytes() == before
    cfg['filters']['rulesets'][-1]['params']['another_parameter'] = "' OR 1=1 --"
    assert len([hit for hit in query(cfg)['hits'] if hit['identity']==identity]) == 2


@pytest.mark.parametrize('sql', ['DELETE FROM messages', 'PRAGMA user_version=3',
                                "ATTACH ':memory:' AS other", "SELECT 'missing' AS identity"])
def test_runtime_query_enforces_read_only_and_identity_membership(config, sql):
    rule = dict(id='unsafe', file='unsafe.sql', sql=sql, params={}, recent_count=0)
    with pytest.raises(ValueError):
        query_rules(dict(provider_id=config['platform'], platform=config['platform'],
                         account_id=config['source']['account_id'], config=config),
                    dict(start_inclusive=100,end_exclusive=111), [rule])
