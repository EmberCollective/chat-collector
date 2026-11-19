"""Synthetic Chaoxing regression; no real account, HTTP, model or notifications."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from integrations.chaoxing_provider.runtime.reception import SchoolReception
from integrations.chaoxing_provider.runtime.storage import bind_account
from integrations.chaoxing_provider.runtime.transport import SourceError
from integrations.chaoxing_provider.selection_entry import configure_provider, load_config
from providers.chaoxing import ChaoxingSnapshot
from providers.query import query_rules, prepare_selected
from providers.query_registry import QUERY_PROVIDERS
from selection import run_round
from selection.rules import load_rulesets
from test_processor import config as processor_config, response


@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.setattr('providers.query_registry.QUERY_PROVIDERS', dict(QUERY_PROVIDERS))
    # Composition deliberately uses the current registry even when tests isolate it.
    import integrations.chaoxing_provider.selection_entry as entry
    import providers.query_registry as registry
    monkeypatch.setattr(entry, 'QUERY_PROVIDERS', registry.QUERY_PROVIDERS)
    configure_provider()
    root = tmp_path / 'capture'
    root.mkdir()
    bind_account(root, 'verified-test-account')
    service = SchoolReception(root)
    database = service.path
    service.close()
    with closing(sqlite3.connect(database)) as db, db:
        db.executemany('INSERT INTO sources(id,kind,target,label,enabled,version,since) VALUES(?,?,?,?,?,?,?)', [
            ('selected', 'chat', 'peer', 'Anonymous', 1, 1, 100),
            ('other', 'chat', 'private', 'Other', 1, 1, 100)])
        db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)', [
            ('new', 'selected', '', 'Submit synthetic report', '', '', 101000, 110, 0, 0),
            ('late', 'selected', '', 'Delayed synthetic report', '', '', 102000, 120, 0, 0),
            ('before', 'selected', '', 'Before consent', '', '', 99000, 110, 0, 0),
            ('baseline', 'selected', '', 'Old baseline', '', '', 103000, 110, 1, 0),
            ('other', 'other', '', 'Private outside scope', '', '', 104000, 110, 0, 0),
            ('image', 'selected', '', '【非文字消息】', '', '', 105000, 110, 0, 0)])
    rule = tmp_path / 'report.sql'
    rule.write_text('SELECT identity FROM candidates WHERE instr(text,:keyword)>0')
    config = dict(platform='chaoxing', provider_id='chaoxing',
                  source=dict(account_id='verified-test-account', database=str(database), source_ids=['selected']),
                  storage=dict(state_file=str(tmp_path/'selection.db'), output_dir=str(tmp_path/'batches')),
                  filters={'rulesets': [{'file': str(rule), 'params': {'keyword': 'report'}}]},
                  schedule={'initial_lookback_seconds': 3600})
    return config


def scope(config):
    return dict(provider_id='chaoxing', platform='chaoxing', account_id=config['source']['account_id'], config=config)


def query(config, start=100, end=120):
    return query_rules(scope(config), dict(start_inclusive=start, end_exclusive=end), load_rulesets(config))


def test_sql_permissions_late_capture_boundaries_and_read_only(capture):
    path = Path(capture['source']['database'])
    before = path.read_bytes()
    result = query(capture)
    assert [row['message']['message_id'] for row in result['messages']] == ['new']
    assert result['messages'][0]['message']['sent_at'] == 101
    assert result['messages'][0]['message']['source']['captured_at'] == 110
    assert len(result['hits']) == 1
    assert [row['message']['message_id'] for row in query(capture, 120, 121)['messages']] == ['late']
    assert path.read_bytes() == before


@pytest.mark.parametrize('change', ['account', 'scope', 'permission', 'empty', 'notice'])
def test_refuse_account_scope_and_permissions(capture, change):
    if change == 'account':
        capture['source']['account_id'] = 'wrong-account'
    elif change == 'scope':
        value = scope(capture)
        value['account_id'] = 'wrong-scope'
        with pytest.raises(ValueError):
            ChaoxingSnapshot(value)
        return
    elif change == 'empty':
        capture['source']['source_ids'] = []
    else:
        with closing(sqlite3.connect(capture['source']['database'])) as db, db:
            if change == 'permission':
                db.execute("UPDATE sources SET enabled=0 WHERE id='selected'")
            else:
                db.execute("UPDATE sources SET kind='notice' WHERE id='selected'")
    with pytest.raises(ValueError):
        query(capture)


def test_revocation_or_new_rows_invalidates_prepared_snapshot(capture):
    result = query(capture)
    with closing(sqlite3.connect(capture['source']['database'])) as db, db:
        db.execute("UPDATE sources SET version=version+1 WHERE id='selected'")
    with pytest.raises(RuntimeError, match='snapshot changed'):
        prepare_selected(capture, [], snapshot_id=result['snapshot_id'])


def test_sql_publish_restart_and_processor_contract(capture, processor_config, monkeypatch):
    from processor import core
    capture['storage']['output_dir'] = processor_config['input']['batches_dir']
    assert run_round(capture, 100, 120)['emitted'] == 1
    assert run_round(capture, 100, 120)['emitted'] == 0
    assert run_round(capture, 120, 121)['emitted'] == 1
    events = [json.loads(line) for path in Path(capture['storage']['output_dir']).glob('*.jsonl')
              for line in path.read_text(encoding='utf-8').splitlines()]
    from contracts import validate_event
    for event in events:
        validate_event(event)
        assert len(event['matched_rules']) == 1
        assert 'Private outside scope' not in json.dumps(event)
    calls = []
    def model(config, rules, group, existing):
        calls.append(group['id'])
        return response(group['primary_message_keys'])
    monkeypatch.setattr(core, '_call_model', model)
    assert core.run_round(processor_config)['tasks_upserted'] == 1
    assert len(calls) == 1
    assert core.list_tasks(processor_config)[0]['conversation']['platform'] == 'chaoxing'


def test_bind_rejects_wrong_account_and_unknown_history(capture, tmp_path):
    root = Path(capture['source']['database']).parents[2]
    with pytest.raises(SourceError):
        bind_account(root, 'other-account')
    with closing(sqlite3.connect(capture['source']['database'])) as db, db:
        db.execute('DELETE FROM capture_metadata')
    with pytest.raises(SourceError) as error:
        bind_account(root, 'verified-test-account')
    assert error.value.code == 'unbound_history_use_new_state'


def test_import_has_no_pipeline_or_network_side_effects():
    code = ('import sys; from providers.query_registry import QUERY_PROVIDERS; '
            'import providers.chaoxing; assert "chaoxing" not in QUERY_PROVIDERS; '
            'assert not any(n.split(".")[0] in {"processor","messaging","tools"} for n in sys.modules)')
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_example_config_valid_and_preview_no_publication(capture, tmp_path):
    original = Path('integrations/chaoxing_provider/selection.example.toml').read_text(encoding='utf-8')
    original = original.replace('replace-with-bootstrap-account-id', 'verified-test-account')
    original = original.replace('replace-with-authorized-source-id', 'selected')
    original = original.replace('../../data/chaoxing/capture/classroom-output/school-reception/inbox.local.sqlite',
                                Path(capture['source']['database']).as_posix())
    original = original.replace('../../data/chaoxing/selection/state.db', (tmp_path/'never.db').as_posix())
    original = original.replace('../../data/batches', (tmp_path/'never-batches').as_posix())
    original = original.replace('../../rulesets/keyword.sql', Path('rulesets/keyword.sql').resolve().as_posix())
    original = original.replace('作业', 'report')
    path = tmp_path / 'source.toml'
    path.write_text(original, encoding='utf-8')
    assert load_config(path)['platform'] == 'chaoxing'
    args = [sys.executable, '-m', 'integrations.chaoxing_provider.selection_entry', '--config', str(path),
            '--start', '100', '--end', '120']
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['matched'] == 1
    assert not (tmp_path/'never.db').exists() and not (tmp_path/'never-batches').exists()
    denied = subprocess.run(args + ['--publish'], capture_output=True, text=True, timeout=20)
    assert denied.returncode != 0 and not (tmp_path/'never.db').exists()
