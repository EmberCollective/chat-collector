"""Public content boundary against a synthetic local provider and temporary archives."""
import hashlib
import json

import pytest

from providers import attachments, content
from test_providers import external


def test_export_then_read_prepares_once_and_reuses_parser_cache(external, monkeypatch):
    config, event, path = external
    from examples.local_file_provider import LocalFileProvider
    prepare = LocalFileProvider.prepare_attachment
    calls = []

    def counted(self, event, index):
        calls.append(index)
        return prepare(self, event, index)

    monkeypatch.setattr(LocalFileProvider, 'prepare_attachment', counted)
    reader = content.create_attachment_context([event], config)
    assert calls == [] and reader.evidence == {}
    material = content.export_attachment(reader, 'f1')
    assert calls == [0] and reader.evidence == {}
    assert material['name'] == 'notice.txt' and material['status'] == 'archived'
    assert material['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert content.read_attachment(reader, 'f1')['details']['status'] == 'ok'
    assert calls == [0]

    def forbidden(*args):
        raise AssertionError('verified archive and parsed cache must be reused')

    monkeypatch.setattr(LocalFileProvider, 'prepare_attachment', forbidden)
    monkeypatch.setattr(attachments, '_run_worker', forbidden)
    archived_event = dict(event, message=dict(event['message'], attachments=[material]))
    again = content.create_attachment_context([archived_event], config)
    reply = content.read_attachment(again, 'f1')
    assert reply['details']['status'] == 'ok'
    assert str(path) not in json.dumps(reply)
    assert again.evidence['f1:b1']['origins'] == [{'message_key': 'm', 'attachment_index': 0}]


def test_material_export_never_parses_or_creates_read_evidence(external, monkeypatch):
    config, event, path = external

    def forbidden(*args):
        raise AssertionError('material export must not parse content')

    monkeypatch.setattr(attachments, '_run_worker', forbidden)
    reader = content.create_attachment_context([event], config)
    material = content.export_attachment(reader, 'f1')
    assert material['archive_path'] == str(path.resolve())
    assert reader.evidence == {} and reader.catalog()[0]['content_status'] == 'unread'
    assert content.export_material(material, config)['sha256'] == material['sha256']
    with pytest.raises(ValueError, match='unknown_attachment_ref'):
        content.export_attachment(reader, str(path))


@pytest.mark.parametrize('restriction,reason', [
    ({'enabled': False}, 'attachment_context_disabled'),
    ({'archive_roots': []}, 'attachment_outside_archive_roots'),
    ({'max_file_bytes': 1}, 'attachment_file_size_limit'),
])
def test_material_revalidation_retains_access_restrictions(external, restriction, reason):
    config, event, path = external
    material = content.export_attachment(content.create_attachment_context([event], config), 'f1')
    config['attachments'].update(restriction)
    result = content.export_material(material, config)
    assert result['status'] == 'unavailable' and result['reason'] == reason
    assert result['name'] == 'notice.txt' and 'archive_path' not in result and 'sha256' not in result


def test_material_hash_change_is_not_exported(external):
    config, event, path = external
    material = content.export_attachment(content.create_attachment_context([event], config), 'f1')
    path.write_text('Changed after task creation.', encoding='utf-8')
    result = content.export_material(material, config, event=event, attachment_index=0)
    assert result['reason'] == 'attachment_archive_hash_mismatch'
    assert 'archive_path' not in result


def test_missing_task_material_can_prepare_from_bound_source(external):
    config, event, path = external
    original = event['message']['attachments'][0]
    result = content.export_material(original, config, event=event, attachment_index=0)
    assert result['archive_path'] == str(path.resolve()) and result['name'] == original['name']
    missing = content.export_material(original, config, event=event, attachment_index=7)
    assert missing['status'] == 'unavailable' and 'archive_path' not in missing


def test_public_content_configuration_keeps_legacy_and_plugin_paths_local(tmp_path):
    settings = {'collector_configs': {'qq': 'qq.toml'}, 'archive_roots': ['archive'], 'cache_dir': 'cache',
                'providers': {'demo': {'factory': 'examples.local_file_provider:LocalFileProvider', 'config': 'demo.toml'}}}
    content.configure_content(settings, tmp_path)
    assert settings['collector_configs'] == {'qq': str(tmp_path / 'qq.toml')}
    assert settings['providers']['demo']['config'] == str(tmp_path / 'demo.toml')
    assert settings['archive_roots'] == [str(tmp_path / 'archive')]
    assert settings['cache_dir'] == str(tmp_path / 'cache')


@pytest.mark.parametrize('settings,error', [
    ({'collector_configs': {'unknown': 'source.toml'}}, 'collector_configs'),
    ({'archive_roots': ['archive'], 'cache_dir': 'archive/cache'}, 'outside original archives'),
])
def test_public_content_configuration_rejects_unsafe_or_unknown_legacy_settings(tmp_path, settings, error):
    with pytest.raises(ValueError, match=error):
        content.configure_content(settings, tmp_path)
