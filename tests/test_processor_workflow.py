import json
from pathlib import Path

import pytest

from processor.workflow import record_round


def test_background_round_records_one_execution_and_safe_metadata(tmp_path):
    config = {'storage': {'state_file': str(tmp_path / 'state.db')}, 'api_key': 'PRIVATE_KEY', '_workflow_origin': 'schedule'}
    calls = []
    @record_round
    def run(settings):
        calls.append(settings)
        progress = Path(settings['_progress_file'])
        folder = progress.parent
        assert json.loads((folder / 'job.json').read_text(encoding='utf-8'))['status'] == 'running'
        progress.with_name('pi-workflow.jsonl').write_text('{"type":"run_end"}\n', encoding='utf-8')
        return {'processed_groups': 2, 'pending_groups': 0, 'tasks_upserted': 3, 'private_result': 'PRIVATE_KEY'}
    result = run(config)
    assert len(calls) == 1 and result['tasks_upserted'] == 3 and '_progress_file' not in config
    metadata = json.loads(next((tmp_path / 'pi-runs').glob('*/job.json')).read_text(encoding='utf-8'))
    assert metadata['status'] == 'succeeded' and metadata['finished_at'] >= metadata['started_at']
    assert metadata['origin'] == 'schedule'
    assert metadata['summary'] == {'processed_groups': 2, 'pending_groups': 0, 'tasks_upserted': 3}
    assert 'PRIVATE_KEY' not in json.dumps(metadata)


def test_background_failure_preserves_records_without_exception_contents(tmp_path):
    @record_round
    def run(settings):
        Path(settings['_progress_file']).with_name('pi-workflow.jsonl').write_text('{"type":"runtime_failed"}\n')
        raise ValueError('PRIVATE EXCEPTION PATH AND SECRET')
    with pytest.raises(ValueError, match='PRIVATE'):
        run({'storage': {'state_file': str(tmp_path / 'state.db')}})
    metadata = json.loads(next((tmp_path / 'pi-runs').glob('*/job.json')).read_text(encoding='utf-8'))
    assert metadata['status'] == 'failed' and 'PRIVATE' not in json.dumps(metadata)


def test_idle_rounds_remove_stubs_and_console_override_is_unchanged(tmp_path):
    calls = []
    @record_round
    def run(settings):
        calls.append(settings)
        return {'processed_groups': 0}
    config = {'storage': {'state_file': str(tmp_path / 'state.db')}}
    run(config)
    assert list((tmp_path / 'pi-runs').iterdir()) == []
    explicit = dict(config, _progress_file=str(tmp_path / 'console' / 'pi-events.jsonl'))
    run(explicit)
    assert calls[-1] is explicit and len(calls) == 2
    assert not (tmp_path / 'console').exists()


def test_history_write_failure_does_not_mask_successful_processing(tmp_path, monkeypatch):
    import processor.workflow as workflow
    original = workflow._write_metadata
    @record_round
    def run(settings):
        Path(settings['_progress_file']).with_name('pi-workflow.jsonl').write_text('{"type":"run_end"}\n')
        return {'tasks_upserted': 1}
    def write(folder, metadata):
        if metadata['status'] == 'running':
            return original(folder, metadata)
        raise OSError('disk full')
    monkeypatch.setattr(workflow, '_write_metadata', write)
    with pytest.warns(RuntimeWarning, match='Pi'):
        assert run({'storage': {'state_file': str(tmp_path / 'state.db')}})['tasks_upserted'] == 1


def test_initial_metadata_failure_cleans_stub_without_executing_processor(tmp_path, monkeypatch):
    import processor.workflow as workflow
    calls = []
    @record_round
    def run(settings):
        calls.append(settings)
    def fail(folder, metadata):
        (folder / 'job.json.tmp').write_text('partial')
        raise OSError('disk full')
    monkeypatch.setattr(workflow, '_write_metadata', fail)
    with pytest.raises(OSError, match='disk full'):
        run({'storage': {'state_file': str(tmp_path / 'state.db')}})
    assert not calls and list((tmp_path / 'pi-runs').iterdir()) == []


def test_cli_and_automation_pass_actual_origin(monkeypatch):
    from processor import __main__ as cli
    from automation import core as automation
    import processor
    seen = []
    monkeypatch.setattr(cli, 'load_config', lambda path: {'schedule': {'interval_seconds': 1}})
    monkeypatch.setattr(cli, 'run_round', lambda config: seen.append(config['_workflow_origin']) or {})
    assert cli.main(['once', '--config', 'fixture']) == 0
    monkeypatch.setattr(processor, 'load_config', lambda path: {})
    monkeypatch.setattr(processor, 'run_round', lambda config: seen.append(config['_workflow_origin']) or {})
    settings = {'jobs': {'processor': {'config': 'fixture'}}}
    automation._callback(settings, 'processor')
    automation._callback(dict(settings, _workflow_origin='manual'), 'processor')
    assert seen == ['cli', 'schedule', 'manual']


def test_automation_episode_and_attempt_reach_pi_metadata(tmp_path, monkeypatch):
    from automation import core
    import processor
    @record_round
    def run(settings):
        Path(settings['_progress_file']).with_name('pi-workflow.jsonl').write_text('{"type":"run_end"}\n')
        return {'tasks_upserted': 1}
    monkeypatch.setattr(processor, 'load_config', lambda _: {'storage': {'state_file': str(tmp_path/'state.db')}})
    monkeypatch.setattr(processor, 'run_round', run)
    core._callback({'jobs': {'processor': {'config':'fixture'}}, '_workflow_parent_run':'a'*32, '_workflow_attempt':2}, 'processor')
    metadata = json.loads(next((tmp_path/'pi-runs').glob('*/job.json')).read_text(encoding='utf8'))
    assert metadata['automation_run_id'] == 'a'*32 and metadata['automation_attempt'] == 2
