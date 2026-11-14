import json
from pathlib import Path

import pytest

from collector.adapters import wechat
from collector.service import collect_once, source_snapshot, publication_path


def config(tmp_path):
    return {'platform': 'wechat', 'source': {'account_id': 'self',
            'decrypted_dir': str(tmp_path / 'plain')},
            'storage': {'state_file': str(tmp_path / 'selection.db')},
            'filters': {'rulesets': [{'file': 'missing.sql'}]}}


def test_collection_without_rules_keeps_selection_and_previous_good_metadata(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    state = Path(cfg['storage']['state_file'])
    state.write_bytes(b'existing selection state')
    monkeypatch.setattr(wechat, 'refresh', lambda source: {'mode': 'fixture'})
    result = collect_once(cfg)
    assert result['refresh'] == {'mode': 'fixture'}
    assert state.read_bytes() == b'existing selection state'
    metadata = publication_path(cfg)
    assert json.loads(metadata.read_text()) == result
    def fail(source):
        raise OSError('refresh failed')
    monkeypatch.setattr(wechat, 'refresh', fail)
    with pytest.raises(OSError):
        collect_once(cfg)
    assert json.loads(metadata.read_text()) == result


def test_snapshot_rejects_external_identity_database_change(tmp_path):
    cfg = config(tmp_path)
    plain = Path(cfg['source']['decrypted_dir'])
    plain.mkdir()
    contact = plain / 'contact.db'
    contact.write_bytes(b'old')
    with pytest.raises(RuntimeError, match='changed'):
        with source_snapshot(cfg):
            contact.write_bytes(b'new contact data')


@pytest.mark.parametrize('name', ['resource.db', 'hardlink.db'])
def test_snapshot_tracks_external_resource_databases(tmp_path, name):
    cfg = config(tmp_path)
    external = tmp_path / 'external'
    external.mkdir()
    cfg['source']['resource_db'] = str(external/'resource.db')
    target = external / name
    target.write_bytes(b'old')
    with pytest.raises(RuntimeError, match='changed'):
        with source_snapshot(cfg) as metadata:
            assert str(target.resolve()) in [row[0] for row in metadata['files']]
            target.write_bytes(b'changed resource')


def test_collection_watermark_is_shared_by_independent_selection_states(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setattr(wechat, 'refresh', lambda source: {'mode': 'fixture'})
    saved = collect_once(cfg)
    other = dict(cfg, storage={'state_file': str(tmp_path/'other-selection.db')})
    with source_snapshot(other) as snapshot:
        assert snapshot['snapshot_id'] == saved['snapshot_id']
        assert snapshot['capture_started_at'] == saved['capture_started_at']
    assert not Path(other['storage']['state_file']).exists()


def test_legacy_collection_record_is_read_then_migrated_without_changing_state(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setattr(wechat, 'refresh', lambda source: {'mode': 'fixture'})
    saved = collect_once(cfg)
    legacy = Path(cfg['storage']['state_file'] + '.collection.json')
    publication_path(cfg).replace(legacy)
    with source_snapshot(cfg) as snapshot:
        assert snapshot['capture_started_at'] == saved['capture_started_at']
    collect_once(cfg)
    assert publication_path(cfg).is_file()
    assert json.loads(legacy.read_text(encoding='utf-8')) == saved
