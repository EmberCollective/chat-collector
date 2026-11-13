import ast
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
