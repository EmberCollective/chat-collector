"""Actual CLI/background composition collects independently of SQL health."""
import copy
import importlib
import json
from pathlib import Path
import sqlite3

import pytest

from application.collection import run_round
from collector.service import publication_path
import selection
from test_filters import config, register


def setup_refresh(config, monkeypatch):
    calls = []
    clock = [111]
    adapter = importlib.import_module('collector.adapters.' + config['platform'])
    monkeypatch.setattr(adapter, 'refresh', lambda source: calls.append(clock[0]) or {'mode': 'fixture'})
    monkeypatch.setattr('collector.service.time.time', lambda: clock[0])
    config['source']['refresh'] = True
    config['schedule']['refresh_interval_seconds'] = 300
    return calls, clock


def test_combined_entry_collects_without_rules_and_throttles_from_collection_progress(config, monkeypatch):
    calls, clock = setup_refresh(config, monkeypatch)
    assert run_round(config)['status'] == 'no_enabled_rules'
    assert publication_path(config).exists() and calls == [111]
    clock[0] = 120
    assert run_round(config)['status'] == 'no_enabled_rules'
    assert calls == [111]
    with sqlite3.connect(config['storage']['state_file']) as db:
        assert db.execute("SELECT value FROM settings WHERE key='window_end'").fetchone() is None


@pytest.mark.parametrize('broken', ['missing.sql', 'malformed.sql'])
def test_bad_rule_does_not_stop_collection_and_fixed_rule_catches_up_while_throttled(config, monkeypatch, tmp_path, broken):
    calls, clock = setup_refresh(config, monkeypatch)
    path = tmp_path/broken
    if broken == 'malformed.sql':
        path.write_text('SELECT missing_column FROM candidates', encoding='utf-8')
    config['filters']['rulesets'] = [{'file': str(path)}]
    with pytest.raises((ValueError, FileNotFoundError)):
        run_round(config)
    assert calls == [111]
    saved = json.loads(publication_path(config).read_text(encoding='utf-8'))
    clock[0] = 120
    config['filters']['rulesets'] = []
    register(config, 'files.sql')
    result = run_round(config)
    assert result['end'] == 111 and result['emitted'] == 1
    assert calls == [111]
    assert json.loads(publication_path(config).read_text(encoding='utf-8')) == saved


def test_same_source_serves_two_scheduled_selection_states(config, monkeypatch, tmp_path):
    calls, _ = setup_refresh(config, monkeypatch)
    register(config, 'files.sql')
    assert run_round(config)['emitted'] == 1
    other = copy.deepcopy(config)
    other['storage'] = {'state_file': str(tmp_path/'other.db'), 'output_dir': str(tmp_path/'other-output')}
    assert selection.run_round(other)['emitted'] == 1
    assert run_round(other)['status'] == 'waiting_for_refresh'
    assert calls == [111]
