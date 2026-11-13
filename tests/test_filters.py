import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from collector.filters import load_rulesets, scan_filtered
from collector.__main__ import run_round


@pytest.fixture(params=['wechat','qq'])
def config(request,tmp_path):
    platform = request.param
    plain = tmp_path/'plain'
    plain.mkdir()
    rows = [(1,90,'anchor',False),(2,100,'plain',False),(3,100,'document',True),
            (4,101,'plain',False),(5,102,'anchor',False),(6,110,'tail',False),(7,120,'tail',False),(8,130,'tail',False),(9,140,'outside',False)]
    if platform=='wechat':
        for shard,selected in enumerate((rows[:3],rows[3:])):
            with sqlite3.connect(plain/f'message_{shard}.db') as db:
                db.execute('CREATE TABLE Name2Id(user_name TEXT)')
                db.executemany('INSERT INTO Name2Id VALUES(?)',[('a@chatroom',),('sender',),('b@chatroom',)])
                for peer in ('a@chatroom','b@chatroom'):
                    table = 'Msg_'+hashlib.md5(peer.encode()).hexdigest()
                    db.execute(f'''CREATE TABLE "{table}"(local_id INTEGER PRIMARY KEY,server_id INTEGER,
                        local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,source BLOB,
                        message_content BLOB,compress_content BLOB)''')
                    data = selected if peer=='a@chatroom' else [(20+shard,100+shard,'unrelated',False)]
                    for identifier,stamp,text,file in data:
                        body = '<msg><appmsg><title>document</title><type>6</type></appmsg></msg>' if file else text
                        db.execute(f'INSERT INTO "{table}" VALUES(?,?,?,2,?,NULL,?,NULL)',(identifier,identifier+1000,(6<<32)|49 if file else 1,stamp,body))
    else:
        from collector.adapters.qq_vendor.c2c_40800_pb2 import MsgBody
        with sqlite3.connect(plain/'nt_msg.db') as db:
            db.execute('''CREATE TABLE group_msg_table("40001" INTEGER PRIMARY KEY,"40050" INTEGER,
                "40030" INTEGER,"40021" TEXT,"40033" INTEGER,"40020" TEXT,"40011" INTEGER,"40800" BLOB)''')
            for peer,selected in ((123,rows),(456,[(20,100,'unrelated',False),(21,101,'unrelated',False)])):
                for identifier,stamp,text,file in selected:
                    body = MsgBody()
                    item = body.content.add(content_type=3 if file else 1,text=text)
                    if file:
                        item.filename='document'
                    db.execute('INSERT INTO group_msg_table VALUES(?,?,?,?,?,?,?,?)',
                               (identifier,stamp,peer,str(peer),789,'sender',1,body.SerializeToString()))
    return {'platform':platform,'source':{'account_id':'account','decrypted_dir':str(plain),'refresh':False},
            'storage':{'state_file':str(tmp_path/'state.db'),'output_dir':str(tmp_path/'batches')},
            'schedule':{'initial_lookback_seconds':100},'filters':{}}


def register(config, name, count=0, **params):
    rule = dict(file=str(Path(__file__).resolve().parents[1]/'rulesets'/name), recent_count=count, params=params)
    config['filters'].setdefault('rulesets', []).append(rule)
    return rule


def selected(config, start=100, end=111):
    return [int(m['source']['local_id']) for m in scan_filtered(config, start, end)]


@pytest.mark.parametrize('file', ['keyword.sql', 'recent-keyword.sql'])
def test_keyword_only_matches_current_message(config, file):
    register(config, file, 20, keyword='anchor')
    rows = list(scan_filtered(config,100,111))
    assert [int(m['source']['local_id']) for m in rows] == [5]
    assert all('anchor' in m['text'] for m in rows)
    assert selected(config,100,101) == []
    config['filters']['rulesets'][0]['params']['keyword'] = ''
    assert selected(config) == []


def test_context_crosses_window_shards_and_ties_but_not_chats(config):
    register(config, 'context.sql', 5)
    assert selected(config) == [1,2,3,4,5,6]
    assert selected(config,100,101) == [1,2,3]
    assert selected(config,101,111) == [1,2,3,4,5,6]
    assert selected(config,120,141) == [1,2,3,4,5,6,7,8]  # Sixth message after the file is excluded.
    assert selected(config,140,141) == []  # No new message within the anchor's radius.
    assert selected(config,90,150) == [1,2,3,4,5,6,7,8]


def test_group_content_is_one_rule_and_does_not_limit_other_rules(config):
    group = 'a@chatroom' if config['platform']=='wechat' else '123'
    register(config, 'group-content.sql', group_id=group, keyword='anchor')
    assert selected(config) == [3,5]
    register(config, 'keyword.sql', keyword='unrelated')
    assert selected(config) == [3,20,21,5]  # Other rules are not constrained to the first rule's group.


def test_files_and_mentions_are_regular_sql_rules(config):
    register(config, 'files.sql')
    assert selected(config) == [3]
    config['filters']['rulesets'].clear()
    register(config, 'mentions-all.sql')
    assert selected(config) == []


def test_three_rules_union_once_before_archive_and_replay(config, monkeypatch):
    register(config, 'keyword.sql', keyword='anchor')
    register(config, 'files-or-all.sql')
    register(config, 'context.sql', 5)
    from collector.assets import AttachmentArchive
    calls = []
    monkeypatch.setattr(AttachmentArchive, 'enrich', lambda self, message: calls.append(message['message_id']) or message)
    first = run_round(config, start=100, end=111)
    assert first['matched'] == first['emitted'] == 6 and len(calls) == len(set(calls)) == 6
    config['filters']['rulesets'].reverse()
    assert selected(config) == [1,2,3,4,5,6]
    second = run_round(config, start=100, end=111)
    assert second['duplicates'] == 6 and second['emitted'] == 0
    with sqlite3.connect(config['storage']['state_file']) as db:
        assert db.execute("SELECT value FROM settings WHERE key='window_end'").fetchone() is None


def test_input_scanned_once_and_history_is_disjoint(config, monkeypatch):
    import importlib
    adapter = importlib.import_module('providers.platforms.'+config['platform'])
    original, calls = adapter.scan, []
    def scan(source, start, end, *args, **kwargs):
        calls.append((start, end))
        return original(source, start, end, *args, **kwargs)
    monkeypatch.setattr(adapter, 'scan', scan)
    register(config, 'keyword.sql', keyword='anchor')
    register(config, 'files-or-all.sql')
    assert selected(config) == [3,5]
    assert calls == [(100,111)]
    register(config, 'context.sql', 5)
    calls.clear()
    selected(config)
    assert calls == [(100,111),(100,100)]


def test_cross_round_context_reselects_history_without_duplicate_events(config):
    register(config, 'context.sql', 5)
    first = run_round(config, start=100, end=101)
    followup = run_round(config, start=101, end=111)
    repeated = run_round(config, start=101, end=111)
    assert first['emitted'] == 3
    assert followup['matched'] == 6 and followup['emitted'] == 3 and followup['duplicates'] == 3
    assert repeated['emitted'] == 0 and repeated['duplicates'] == 6
    events = [json.loads(line) for path in Path(config['storage']['output_dir']).glob('*.jsonl')
              for line in path.read_text(encoding='utf-8').splitlines()]
    assert len({e['message_key'] for e in events}) == len(events) == 6


def test_rule_failure_before_attachment_or_checkpoint(config, tmp_path, monkeypatch):
    register(config, 'all.sql')
    broken = tmp_path/'broken.sql'
    broken.write_text("SELECT json_extract('invalid json','$') AS identity FROM candidates")
    config['filters']['rulesets'].append({'file':str(broken)})
    from collector.assets import AttachmentArchive
    calls = []
    monkeypatch.setattr(AttachmentArchive, 'enrich', lambda self, message: calls.append(message) or message)
    monkeypatch.setattr('collector.__main__.time.time', lambda:111)
    with pytest.raises(ValueError, match='broken.sql'):
        run_round(config)
    assert calls == []
    with sqlite3.connect(config['storage']['state_file']) as db:
        assert db.execute('SELECT count(*) FROM seen').fetchone()[0] == 0
        assert db.execute("SELECT value FROM settings WHERE key='window_end'").fetchone() is None


@pytest.mark.parametrize('sql', ["DELETE FROM messages", "ATTACH DATABASE ':memory:' AS other",
                                'PRAGMA query_only=OFF', "SELECT load_extension('x')",
                                'SELECT identity FROM candidates; SELECT 1'])
def test_sql_is_read_only_and_single_statement(tmp_path, sql):
    path = tmp_path/'rule.sql'
    path.write_text(sql)
    with pytest.raises(ValueError):
        load_rulesets({'filters':{'rulesets':[{'file':str(path)}]}})


@pytest.mark.parametrize('sql', ["SELECT 'missing' AS identity", 'SELECT NULL AS identity',
                                'SELECT message_id FROM candidates'])
def test_bad_result_fails_instead_of_silent_empty(config, tmp_path, sql):
    path = tmp_path/'invalid-result.sql'
    path.write_text(sql)
    config['filters']['rulesets'] = [{'file':str(path)}]
    with pytest.raises(ValueError):
        selected(config)


def test_parameters_are_bound_and_source_is_read_only(config):
    before = {p:p.read_bytes() for p in Path(config['source']['decrypted_dir']).glob('*.db')}
    register(config, 'keyword.sql', keyword="' OR 1=1 --")
    assert selected(config) == []
    assert all(p.read_bytes() == data for p,data in before.items())


def test_relative_registration_and_reserved_params(tmp_path):
    (tmp_path/'rule.sql').write_text('SELECT identity FROM candidates WHERE sent_at < :window_end')
    config = {'_config_path':str(tmp_path/'config.toml'), 'filters':{'rulesets':[{'file':'rule.sql'}]}}
    assert load_rulesets(config)[0]['file'] == str(tmp_path/'rule.sql')
    config['filters']['rulesets'][0]['params'] = {'window_end':999}
    with pytest.raises(ValueError, match='reserved'):
        load_rulesets(config)


def test_retired_json_rules_fail_explicitly():
    with pytest.raises(ValueError, match='retired'):
        load_rulesets({'filters':{'rules':[{'type':'nearby'}]}})


def test_three_registrations_can_be_enabled_independently(config):
    mention = register(config, 'mentions-all.sql')
    files = register(config, 'files.sql')
    context = register(config, 'context.sql', 5)
    for rule in (mention, files, context):
        rule['enabled'] = False
    assert selected(config) == []
    mention['enabled'] = True
    assert selected(config) == []
    files['enabled'] = True
    assert selected(config) == [3]
    context['enabled'] = True
    assert selected(config) == [1,2,3,4,5,6]
    files['enabled'] = mention['enabled'] = False
    assert selected(config) == [1,2,3,4,5,6]


def test_disabled_rules_do_not_refresh_or_advance_checkpoint(config, monkeypatch):
    rule = register(config, 'files.sql')
    monkeypatch.setattr('collector.__main__.time.time', lambda:111)
    assert run_round(config)['emitted'] == 1
    rule['enabled'] = False
    config['source']['refresh'] = True
    import importlib
    adapter = importlib.import_module('providers.platforms.'+config['platform'])
    def unexpected(*args, **kwargs):
        pytest.fail('Disabled rules must not scan or refresh')
    monkeypatch.setattr(importlib.import_module('collector.adapters.'+config['platform']), 'refresh', unexpected)
    monkeypatch.setattr(adapter, 'scan', unexpected)
    monkeypatch.setattr('collector.__main__.time.time', lambda:200)
    assert run_round(config)['status'] == 'no_enabled_rules'
    assert selected(config) == []
    config['filters']['rulesets'] = []
    assert run_round(config)['status'] == 'no_enabled_rules'
    assert selected(config) == []
    with sqlite3.connect(config['storage']['state_file']) as db:
        assert db.execute("SELECT value FROM settings WHERE key='window_end'").fetchone()[0] == '111'


def test_disabled_file_is_not_loaded_and_enabled_is_boolean(tmp_path):
    rule = {'file':str(tmp_path/'missing.sql'),'enabled':False}
    assert load_rulesets({'filters':{'rulesets':[rule]}}) == []
    rule['enabled'] = 'false'
    with pytest.raises(ValueError, match='enabled'):
        load_rulesets({'filters':{'rulesets':[rule]}})


def test_all_rule_matches_survive_union_without_duplicate_messages(config):
    register(config, 'all.sql')
    register(config, 'files.sql')
    register(config, 'files.sql')
    register(config, 'keyword.sql', keyword='document')
    register(config, 'keyword.sql', keyword='doc')
    rows = list(scan_filtered(config, 100, 111))
    document = next(row for row in rows if row['kind']=='file')
    rules = document['matched_rules']
    assert len(rules) == 4  # Identical registrations collapse; different parameters remain.
    assert {Path(rule['file']).name for rule in rules} == {'all.sql','files.sql','keyword.sql'}
    assert {rule['params']['keyword'] for rule in rules if 'keyword' in rule['params']} == {'document','doc'}
    assert all(len(rule['sql_sha256']) == 64 for rule in rules)
    config['filters']['rulesets'].reverse()
    assert rows == list(scan_filtered(config, 100, 111))


def test_rule_sql_version_is_captured(config, tmp_path):
    path = tmp_path/'versioned.sql'
    path.write_text('SELECT identity FROM candidates')
    config['filters']['rulesets'] = [{'file':str(path)}]
    before = next(scan_filtered(config,100,111))['matched_rules'][0]
    path.write_text('SELECT identity FROM candidates WHERE 1')
    after = next(scan_filtered(config,100,111))['matched_rules'][0]
    assert before['file'] == after['file'] and before['sql_sha256'] != after['sql_sha256']


@pytest.mark.parametrize('trigger', ['file', 'mentions_all'])
def test_context_exactly_five_each_side_and_new_followups(config, monkeypatch, trigger):
    import importlib
    adapter = importlib.import_module('providers.platforms.'+config['platform'])
    records = [dict(platform=config['platform'],account_id='account',conversation_id='room',
        conversation_type='group',message_id=str(i),sender_id='sender',sent_at=i,
        text='plain',kind='file' if trigger=='file' and i==11 else 'text',
        mentions_all=trigger=='mentions_all' and i==11,attachments=[],
        source={'local_id':i,'database':'fixture.db'}) for i in range(1,23)]
    def scan(source,start,end,*,context=None,conversations=None):
        rows = [m for m in records if start <= m['sent_at'] < end]
        if context:
            rows = [m for m in records if m['sent_at'] < start][-context[0]:]+rows
        return rows
    monkeypatch.setattr(adapter,'scan',scan)
    register(config,'context.sql',5)
    assert selected(config,1,23) == list(range(6,17))
    assert selected(config,11,12) == list(range(6,12))
    assert selected(config,12,15) == list(range(6,15))
    assert selected(config,15,23) == list(range(6,17))
    assert selected(config,16,23) == list(range(6,17))  # Anchor at the history boundary retains its preceding five.
    assert selected(config,17,23) == []
    config['filters']['rulesets'][0]['enabled'] = False
    register(config,'keyword.sql',keyword='plain')
    assert selected(config,11,12) == [11]  # Ordinary keyword SQL never emits older context.
