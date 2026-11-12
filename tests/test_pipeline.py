import json
from pathlib import Path
import sqlite3

import pytest

from collector.config import build_filter, load_config
from collector.store import Store
from collector.__main__ import InstanceLock


def message(text='hello', platform='wechat', conversation='group-1'):
    return dict(platform=platform, account_id='account', conversation_id=conversation,
                message_id='server:12', sender_id='sender', sent_at=100, kind='text',
                text=text, mentions_all=False, attachments=[], source={'database': 'message_0.db'})


def test_replay_edit_and_revert_are_distinct_revisions(tmp_path):
    store = Store(tmp_path / 'state.db', 'wechat', 'account')
    try:
        assert store.ingest([message()], 0, 200, 'now')['emitted'] == 1
        assert store.ingest([message()], 0, 200, 'later')['emitted'] == 0
        assert store.ingest([message('edited')], 0, 200, 'later')['emitted'] == 1
        assert store.ingest([message()], 0, 200, 'later')['emitted'] == 1
        batches = store.export(tmp_path / 'out')
        records = [json.loads(line) for line in Path(batches[0]).read_text().splitlines()]
        assert [record['revision'] for record in records] == [1, 2, 3]
        assert len({record['event_id'] for record in records}) == 3
        assert store.export(tmp_path / 'out') == []
    finally:
        store.close()


def test_identical_text_in_another_conversation_is_preserved(tmp_path):
    store = Store(tmp_path / 'state.db', 'wechat', 'account')
    try:
        assert store.ingest([message(), message(conversation='group-2')], 0, 200, 'now')['emitted'] == 2
    finally:
        store.close()


def test_failed_scan_rolls_back_events_and_window(tmp_path):
    def broken():
        yield message()
        raise OSError('snapshot unavailable')
    store = Store(tmp_path / 'state.db', 'wechat', 'account')
    try:
        with pytest.raises(OSError):
            store.ingest(broken(), 0, 200, 'now')
        assert store.get('window_end') is None
        assert store.db.execute('SELECT count(*) FROM seen').fetchone()[0] == 0
        assert store.db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
    finally:
        store.close()


def test_restart_recovers_durable_unexported_records(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path, 'wechat', 'account')
    store.ingest([message()], 0, 200, 'now')
    store.close()
    store = Store(path, 'wechat', 'account')
    try:
        assert store.get('window_end') == '200'
        batches = store.export(tmp_path / 'out')
        # Simulate crash after file replacement, before exported flag commit.
        with store.db:
            store.db.execute('UPDATE outbox SET exported=0')
        assert store.export(tmp_path / 'out') == batches
        assert len(list((tmp_path / 'out').glob('*.jsonl'))) == 1
    finally:
        store.close()


def test_conflicting_existing_batch_is_not_overwritten(tmp_path):
    store = Store(tmp_path / 'state.db', 'wechat', 'account')
    try:
        store.ingest([message()], 0, 200, 'now')
        path = Path(store.export(tmp_path / 'out')[0])
        path.write_text('different data')
        with store.db:
            store.db.execute('UPDATE outbox SET exported=0')
        with pytest.raises(RuntimeError):
            store.export(tmp_path / 'out')
        assert path.read_text() == 'different data'
    finally:
        store.close()


def test_explicit_replay_does_not_move_scheduled_checkpoint(tmp_path):
    store = Store(tmp_path / 'state.db', 'wechat', 'account')
    try:
        store.ingest([], 100, 200, 'now')
        store.ingest([message()], 0, 1000, 'later', advance=False)
        assert store.get('window_end') == '200'
    finally:
        store.close()


def test_sql_builtins_group_and_any_content():
    sql, values = build_filter({'filters': {'conversation_ids': ['g1'], 'mentions_all': True,
                                         'files': True, 'keywords': ["it's due"]}})
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE messages(conversation_id,text,kind,mentions_all)')
    conn.executemany('INSERT INTO messages VALUES (?,?,?,?)', [('g1', "it's due", 'text', 0),
                        ('g1', 'other', 'file', 0), ('g2', 'all', 'text', 1), ('g1', 'other', 'text', 0)])
    assert len(conn.execute('SELECT * FROM messages WHERE ' + sql, values).fetchall()) == 2
    conn.close()


def test_sql_and_code_extensions(tmp_path):
    sql_file = tmp_path / 'filter.sql'
    sql_file.write_text('sender_id = :sender')
    code = tmp_path / 'filter.py'
    code.write_text("def build_filter(settings):\n    return 'length(text) >= :minimum', {'minimum': 3}\n")
    sql, params = build_filter({'filters': {'sql_file': str(sql_file), 'code_file': str(code), 'params': {'sender': 'u1'}}})
    conn = sqlite3.connect(':memory:')
    assert conn.execute('SELECT 1 FROM (SELECT :sender AS sender_id, \'hello\' AS text) WHERE ' + sql, params).fetchone() == (1,)
    conn.close()


def test_reserved_parameter_and_bad_match_rejected():
    with pytest.raises(ValueError):
        build_filter({'filters': {'params': {'_wc_start': 0}}})
    with pytest.raises(ValueError):
        build_filter({'filters': {'match': 'sometimes'}})


def test_state_cannot_change_account(tmp_path):
    path = tmp_path / 'state.db'
    Store(path, 'wechat', 'a').close()
    with pytest.raises(ValueError):
        Store(path, 'qq', 'a')


def test_single_writer_lock(tmp_path):
    with InstanceLock(tmp_path / 'state'):
        with pytest.raises(RuntimeError):
            with InstanceLock(tmp_path / 'state'):
                pass
    with InstanceLock(tmp_path / 'state'):
        pass


def test_relative_config_paths_and_schedule_validation(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('''platform="wechat"
[source]
account_id="a"
decrypted_dir="plain"
[storage]
state_file="state.db"
output_dir="batches"
''')
    config = load_config(path)
    assert config['source']['decrypted_dir'] == str(tmp_path / 'plain')
    assert config['schedule']['interval_seconds'] == 60
    assert config['schedule']['refresh_interval_seconds'] == 0
    assert config['processing']['intensity'] == 'full'


@pytest.mark.parametrize('setting', ['[processing]\nintensity="turbo"', '[processing]\nintensity=true',
                                   '[processing]\nintensity=[]', '[schedule]\nrefresh_interval_seconds=-1',
                                   '[schedule]\nrefresh_interval_seconds=true'])
def test_invalid_processing_settings_rejected(tmp_path, setting):
    path = tmp_path/'config.toml'
    path.write_text('platform="wechat"\n[source]\naccount_id="a"\n[storage]\nstate_file="state.db"\noutput_dir="out"\n'+setting)
    with pytest.raises(ValueError):
        load_config(path)


def test_refresh_interval_defers_without_losing_window_and_replay_bypasses(tmp_path, monkeypatch):
    from collector import __main__ as runner
    from collector.adapters import wechat
    calls = []
    cfg = {'platform':'wechat', 'source':{'account_id':'account','refresh':True},
           'storage':{'state_file':str(tmp_path/'state.db'),'output_dir':str(tmp_path/'out')},
           'schedule':{'initial_lookback_seconds':100,'refresh_interval_seconds':300},
           'processing':{'intensity':'metadata'}}
    monkeypatch.setattr(wechat, 'refresh', lambda source: calls.append('refresh') or {})
    def scan(source, start, end, where, params):
        calls.append((start, end))
        return [message()] if start <= 100 < end else []
    monkeypatch.setattr(wechat, 'scan', scan)
    monkeypatch.setattr(runner.time, 'time', lambda: 200)
    assert runner.run_round(cfg)['matched'] == 1
    calls.clear()
    monkeypatch.setattr(runner.time, 'time', lambda: 250)
    assert runner.run_round(cfg)['status'] == 'waiting_for_refresh'
    assert calls == []
    with sqlite3.connect(tmp_path/'state.db') as conn:
        assert conn.execute("SELECT value FROM settings WHERE key='window_end'").fetchone()[0] == '200'
    assert runner.run_round(cfg, start=100, end=200)['duplicates'] == 1
    assert calls == ['refresh', (100, 200)]
    calls.clear()
    monkeypatch.setattr(runner.time, 'time', lambda: 500)
    runner.run_round(cfg)
    assert calls == ['refresh', (200, 500)]


@pytest.mark.parametrize('name', ['files', 'mentions_all'])
def test_string_false_is_not_a_boolean(name):
    with pytest.raises(ValueError):
        build_filter({'filters': {name: 'false'}})


def test_state_input_collision_rejected_before_open(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('''platform="qq"
[source]
account_id="a"
database="messages.db"
[storage]
state_file="messages.db"
output_dir="batches"
''')
    with pytest.raises(ValueError, match='input database'):
        load_config(path)
    assert not (tmp_path / 'messages.db').exists()


def test_state_inside_source_dir_is_rejected(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('''platform="wechat"
[source]
account_id="a"
decrypted_dir="plain"
[storage]
state_file="plain/message_0.db"
output_dir="batches"
''')
    with pytest.raises(ValueError, match='outside source'):
        load_config(path)


def test_wrong_account_record_rolls_back(tmp_path):
    store = Store(tmp_path / 'state.db', 'wechat', 'other-account')
    try:
        with pytest.raises(ValueError, match='does not match'):
            store.ingest([message()], 0, 200, 'now')
        assert store.get('window_end') is None
    finally:
        store.close()
