import ast
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import selection
from test_filters import config, register


def test_collector_service_import_does_not_load_consumers():
    result = subprocess.run([sys.executable, '-c',
        'import sys; import collector.service; from collector.adapters import qq, wechat; '
        'assert not any(k.split(".")[0] in {"selection", "processor", "messaging", "providers"} '
        'for k in sys.modules)'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_legacy_scans_delegate_to_provider_mapping(monkeypatch):
    from collector.adapters import qq, wechat
    from providers.platforms import qq as qq_provider, wechat as wechat_provider
    for legacy, provider in ((qq, qq_provider), (wechat, wechat_provider)):
        monkeypatch.setattr(provider, 'scan', lambda *args, **kwargs: ['mapped'])
        assert legacy.scan({}, 1, 2) == ['mapped']
    assert wechat.decode_text is wechat_provider.decode_text
    assert qq._body is qq_provider._body


def test_attachment_preparation_is_provider_owned_and_legacy_import_is_compatible():
    from collector.assets import AttachmentArchive as legacy
    from providers.platforms.assets import AttachmentArchive
    from providers.platforms import attachments
    assert legacy is attachments.AttachmentArchive is AttachmentArchive
    assert AttachmentArchive.__module__ == 'providers.platforms.assets'


def test_selection_has_no_platform_mapping_or_message_database_code():
    root = Path(selection.__file__).parent
    for path in root.glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or '').startswith('collector'), path
            if isinstance(node, ast.Import):
                assert not any(name.name.startswith('collector') for name in node.names), path
                if path.name != 'store.py':
                    assert all(name.name != 'sqlite3' for name in node.names), path


def test_selection_can_replay_without_collecting(config, monkeypatch):
    import importlib
    adapter = importlib.import_module('collector.adapters.' + config['platform'])
    monkeypatch.setattr(adapter, 'refresh', lambda source: pytest.fail('Selection cannot refresh'))
    config['source']['refresh'] = True
    register(config, 'files.sql')
    assert selection.run_round(config, 100, 111)['emitted'] == 1
    assert selection.run_round(config, 100, 111)['duplicates'] == 1
    with sqlite3.connect(config['storage']['state_file']) as db:
        assert db.execute("SELECT value FROM settings WHERE key='window_end'").fetchone() is None


def test_replay_publishes_changed_continuity_after_an_unselected_late_row(config):
    register(config, 'keyword.sql', keyword='plain')
    folder = Path(config['source']['decrypted_dir'])
    if config['platform'] == 'qq':
        path, table, column = folder/'nt_msg.db', 'group_msg_table', '40001'
    else:
        path, column = folder/'message_0.db', 'local_id'
        with closing(sqlite3.connect(path)) as db:
            table = next(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'Msg_%'")
                         if db.execute(f'SELECT count(*) FROM "{row[0]}" WHERE local_id=3').fetchone()[0])
    with closing(sqlite3.connect(path)) as db, db:
        late = db.execute(f'SELECT * FROM "{table}" WHERE "{column}"=3').fetchone()
        db.execute(f'DELETE FROM "{table}" WHERE "{column}"=3')
    assert selection.run_round(config, 100, 102)['emitted'] == 2
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f'INSERT INTO "{table}" VALUES({",".join("?" for _ in late)})', late)
    replay = selection.run_round(config, 100, 102)
    assert replay['emitted'] == replay['duplicates'] == 1
    events = [json.loads(line) for batch in Path(config['storage']['output_dir']).glob('*.jsonl')
              for line in batch.read_text(encoding='utf-8').splitlines()]
    revisions = sorted((event for event in events if int(event['message']['source']['local_id']) == 4),
                       key=lambda event: event['revision'])
    assert [event['revision'] for event in revisions] == [1, 2]
    assert revisions[0]['message']['source']['selection_context'] != revisions[1]['message']['source']['selection_context']
    from processor.core import _sql_groups
    latest = {event['message_key']: event for event in sorted(events, key=lambda event: event['revision'])}
    processor = dict(input={'start_at': 0}, context={'max_messages': 20, 'max_conversation_chars': 10000},
                     source_descriptions={}, model={'base_url': '', 'model': 'fake', 'vision': False})
    first = events[0]['message']
    groups = _sql_groups(tuple(first[key] for key in ('platform', 'account_id', 'conversation_id')),
                         list(latest.values()), processor, {'sha256': 'fixture'})
    assert len(groups) == 1 and [len(segment) for segment in groups[0]['segments']] == [1, 1]
    assert selection.run_round(config, 100, 102)['emitted'] == 0


def test_publication_failure_recovers_committed_window_without_query(config, monkeypatch):
    register(config, 'files.sql')
    monkeypatch.setattr(selection.time, 'time', lambda: 111)
    original = selection.Store.export
    def fail_pending(self, folder):
        if self.db.execute('SELECT count(*) FROM outbox WHERE exported=0').fetchone()[0]:
            raise OSError('publication interrupted')
        return original(self, folder)
    monkeypatch.setattr(selection.Store, 'export', fail_pending)
    with pytest.raises(OSError, match='interrupted'):
        selection.run_round(config)
    with sqlite3.connect(config['storage']['state_file']) as db:
        before = db.execute('SELECT body FROM outbox').fetchone()[0]
        assert db.execute("SELECT value FROM settings WHERE key='window_end'").fetchone()[0] == '111'
    monkeypatch.setattr(selection.Store, 'export', original)
    monkeypatch.setattr(selection, 'query_rules', lambda *args: pytest.fail('Recovered window cannot requery'))
    result = selection.run_round(config)
    assert result['status'] == 'no_new_time_window'
    assert len(result['batches']) == 1
    assert json.loads(Path(result['batches'][0]).read_text(encoding='utf-8')) == json.loads(before)


def test_archive_enrichment_rejects_changed_source_before_publication(config, monkeypatch):
    from collector.assets import AttachmentArchive
    register(config, 'files.sql')
    def enrich(self, message):
        (Path(config['source']['decrypted_dir'])/'contact.db').write_bytes(b'external modification')
        return message
    monkeypatch.setattr(AttachmentArchive, 'enrich', enrich)
    with pytest.raises(RuntimeError, match='changed'):
        selection.run_round(config, 100, 111)
    with sqlite3.connect(config['storage']['state_file']) as db:
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
        assert db.execute("SELECT value FROM settings WHERE key='window_end'").fetchone() is None


def test_refresh_between_query_and_archive_retries_without_publication(config, monkeypatch):
    register(config, 'files.sql')
    original = selection.query_rules
    def query_then_change(*args):
        result = original(*args)
        (Path(config['source']['decrypted_dir'])/'contact.db').write_bytes(b'new generation')
        return result
    monkeypatch.setattr(selection, 'query_rules', query_then_change)
    with pytest.raises(RuntimeError, match='changed after query'):
        selection.run_round(config, 100, 111)
    with sqlite3.connect(config['storage']['state_file']) as db:
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
        assert db.execute("SELECT value FROM settings WHERE key='window_end'").fetchone() is None


def test_scheduled_selection_requires_managed_source_watermark_but_replay_works(config):
    config['source']['refresh'] = True
    register(config, 'files.sql')
    with pytest.raises(RuntimeError, match='watermark'):
        selection.run_round(config)
    assert selection.run_round(config, 100, 111)['emitted'] == 1


def test_watermark_is_rechecked_with_query_generation_before_commit(config, monkeypatch):
    from collector.service import collect_once, publication_path
    register(config, 'files.sql')
    monkeypatch.setattr(selection.time, 'time', lambda: 111)
    collect_once(config)
    original = selection.query_rules
    def query_then_lower_watermark(*args):
        result = original(*args)
        path = publication_path(config)
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved['capture_started_at'] = 100
        path.write_text(json.dumps(saved), encoding='utf-8')
        return result
    monkeypatch.setattr(selection, 'query_rules', query_then_lower_watermark)
    with pytest.raises(RuntimeError, match='watermark changed'):
        selection.run_round(config)
    with sqlite3.connect(config['storage']['state_file']) as db:
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
        assert db.execute("SELECT value FROM settings WHERE key='window_end'").fetchone() is None


def test_scheduled_selection_stops_at_capture_start_and_keeps_later_arrivals(config, monkeypatch):
    import importlib
    from collector.service import collect_once
    register(config, 'files.sql')
    config['source']['refresh'] = True
    adapter = importlib.import_module('collector.adapters.'+config['platform'])
    clock = [100]
    monkeypatch.setattr(selection.time, 'time', lambda: clock[0])
    def refresh(source):
        if clock[0] == 140:
            folder = Path(source['decrypted_dir'])
            if config['platform'] == 'qq':
                with sqlite3.connect(folder/'nt_msg.db') as db:
                    db.execute('''INSERT INTO group_msg_table SELECT
                        99,105,"40030","40021","40033","40020","40011","40800"
                        FROM group_msg_table WHERE "40001"=3''')
            else:
                with sqlite3.connect(folder/'message_0.db') as db:
                    table = db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'Msg_%' ORDER BY name").fetchall()
                    table = next(row[0] for row in table if db.execute(f'SELECT count(*) FROM "{row[0]}" WHERE local_id=3').fetchone()[0])
                    db.execute(f'''INSERT INTO "{table}" SELECT 99,1099,local_type,real_sender_id,
                        105,source,message_content,compress_content FROM "{table}" WHERE local_id=3''')
        clock[0] += 30
        return {'mode':'fixture'}
    monkeypatch.setattr(adapter, 'refresh', refresh)
    first = collect_once(config)
    assert first['capture_started_at'] == 100 and first['collected_at'] == 130
    assert selection.run_round(config)['end'] == 100
    clock[0] = 140
    second = collect_once(config)
    assert second['capture_started_at'] == 140 and second['collected_at'] == 170
    assert selection.run_round(config)['end'] == 140
    events = [json.loads(line) for batch in Path(config['storage']['output_dir']).glob('*.jsonl')
              for line in batch.read_text(encoding='utf-8').splitlines()]
    assert any(int(event['message']['source']['local_id']) == 99 for event in events)
