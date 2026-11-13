import hashlib
from pathlib import Path

import pytest

from messaging.package import archive_result, build_package, material_path


def fixture(tmp_path):
    root = tmp_path/'archive'
    root.mkdir()
    original = root/'report.pdf'
    original.write_bytes(b'fixture-only-file')
    sha = hashlib.sha256(original.read_bytes()).hexdigest()
    attachment = {'name': 'report.pdf', 'type': 'file', 'archive_path': str(original), 'sha256': sha, 'status': 'archived'}
    config = {'storage': {'assets_dir': str(tmp_path/'materials')}, 'attachments': {'allowed_roots': [str(root)]}}
    task = {'task_id': 't1', 'revision': 1, 'title': 'Submit report', 'status': 'open', 'action_steps': ['Fill the form'],
            'materials': [{'message_key': 'm1', 'attachment_index': 0, 'attachment': attachment}],
            'sources': [{'message_key': 'm1', 'collector_module': 'qq', 'sender': 'Teacher', 'text': 'https://example.com/form',
                         'attachments': [attachment], 'source': {'database': 'PRIVATE.db'},
                         'identity': {'conversation': {'name': 'Class'}}}]}
    return config, task, original


def test_materials_are_read_only_in_preview_durable_and_deduplicated_on_prepare(tmp_path):
    config, task, original = fixture(tmp_path)
    package = build_package(task, config)
    assert not Path(config['storage']['assets_dir']).exists()
    assert package['resources'][0]['status'] == 'ready'
    task['sources'].append(dict(task['sources'][0], message_key='m2'))
    task['materials'].append(dict(task['materials'][0], message_key='m2'))
    package = build_package(task, config, prepare=True)
    assert len(package['resources']) == 2  # one file and one URL
    assert package['resources'][0]['source_refs'] == ['m1', 'm2']
    path, name = material_path(package, package['resources'][0]['id'], config)
    assert path.read_bytes() == original.read_bytes()
    assert name == 'report.pdf'
    original.unlink()
    again = build_package(task, config, prepare=True)
    assert again['resources'][0]['status'] == 'ready'
    assert again['resources'][0]['local_path'] == str(path)
    assert 'PRIVATE.db' not in str(package)


def test_resolved_evidence_wins_over_old_missing_metadata(tmp_path):
    config, task, original = fixture(tmp_path)
    resolved = dict(task['sources'][0]['attachments'][0])
    task['sources'][0]['attachments'] = [{'name': 'report.pdf', 'status': 'missing'}]
    task['attachment_evidence'] = [{'message_key': 'm1', 'attachment_index': 0, 'attachment': resolved}]
    assert build_package(task, config)['resources'][0]['status'] == 'ready'


def test_materials_reject_path_escape_hash_mismatch_and_encrypted_bytes(tmp_path):
    config, task, original = fixture(tmp_path)
    config['attachments']['allowed_roots'] = []
    assert build_package(task, config, prepare=True)['resources'][0]['status'] == 'unavailable'
    config['attachments']['allowed_roots'] = [str(original.parent)]
    original.write_bytes(b'changed')
    assert build_package(task, config)['resources'][0]['warnings'] == ['material_hash_mismatch']
    task['sources'][0]['attachments'][0]['status'] = 'encrypted_only'
    assert build_package(task, config)['resources'][0]['status'] == 'unavailable'


def test_result_files_survive_completion_and_download_is_registered_only(tmp_path):
    config, _, original = fixture(tmp_path)
    resource = archive_result(original, config)
    assert resource['role'] == 'result'
    package = {'status': 'completed', 'resources': [resource]}
    assert material_path(package, resource['id'], config)[0].is_file()
    with pytest.raises(ValueError):
        material_path(package, '../../.env', config)
    original.with_name('.env').write_text('secret')
    with pytest.raises(ValueError):
        archive_result(original.with_name('.env'), config)


def test_links_only_come_from_cited_messages_and_names_never_include_paths(tmp_path):
    config, task, _ = fixture(tmp_path)
    task['event'] = 'invented https://bad.invalid/'
    task['sources'][0]['text'] = '请在 https://example.com/form。完成。 file:///C:/secret https://user:pass@example.com'
    task['sources'][0]['attachments'][0]['name'] = 'C:\\private\\report.pdf'
    package = build_package(task, config)
    assert [r['url'] for r in package['resources'] if r['type'] == 'link'] == ['https://example.com/form']
    assert package['resources'][0]['name'] == 'report.pdf'


def test_only_explicit_materials_and_cited_evidence_can_be_uploaded(tmp_path):
    config, task, _ = fixture(tmp_path)
    task['sources'][0]['attachments'].append({'name': 'unrelated-private.pdf', 'type': 'file'})
    package = build_package(task, config, prepare=True)
    assert package['unselected_materials_count'] == 1
    assert 'unrelated-private.pdf' not in str(package)
    task.pop('materials')
    package = build_package(task, config)
    assert package['unselected_materials_count'] == 2
    assert all(r['type'] == 'link' for r in package['resources'])


def test_legacy_task_defaults_form_a_valid_outbound_package(tmp_path):
    from messaging.channels import public_envelope
    config, task, _ = fixture(tmp_path)
    task.update(priority='P2', event='Submit report')
    package = build_package(task, config)
    outbound = public_envelope({'delivery_id': 'd1', 'kind': 'upsert', 'task': package})
    assert outbound['task']['needs_review'] is False
    assert outbound['task']['people'] == []
    assert 'local_path' not in str(outbound)
