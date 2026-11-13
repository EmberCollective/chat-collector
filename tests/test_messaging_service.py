import json
from pathlib import Path

import pytest

from processor import load_config as load_processor, storage, service
from messaging import core, load_config, sync, preview, set_task_state


@pytest.fixture
def configs(tmp_path):
    (tmp_path / 'rules.md').write_text('Rules')
    path = tmp_path / 'processor.toml'
    path.write_text('''[input]
batches_dir="chat"
[storage]
state_file="processor.db"
output_dir="todos"
[rules]
file="rules.md"
''')
    task_config = load_processor(path)
    message_path = tmp_path / 'messaging.toml'
    message_path.write_text('''[input]
processor_config="processor.toml"
batches_dir="todos"
[storage]
state_file="messaging.db"
assets_dir="assets"
''')
    return task_config, load_config(message_path)


def save(config, identifier, **fields):
    task = dict(task_id=identifier, priority='P2', status='open', title='Report', event='Submit report', sources=[])
    task.update(fields)
    with storage._connect(config, write=True) as conn:
        storage._save_task(conn, task)
    return task


def test_service_consumes_unexported_changes_and_commits_cursor_with_projection(configs):
    tasks, messages = configs
    save(tasks, 't1')
    assert not Path(tasks['storage']['output_dir']).exists()
    assert preview(messages)['pending_events'] == 1
    assert not Path(messages['storage']['state_file']).exists()
    assert sync(messages)['imported_events'] == 1
    assert sync(messages)['imported_events'] == 0
    with core._connect(messages) as conn:
        before = conn.execute('SELECT cursor FROM source_cursors').fetchone()[0]
    # One valid change followed by an invalid event must not consume either cursor.
    save(tasks, 't2')
    with storage._connect(tasks, write=True) as conn:
        conn.execute('INSERT INTO outbox(body) VALUES(?)', ('{}',))
    with pytest.raises(ValueError, match='Invalid task event'):
        sync(messages)
    with core._connect(messages) as conn:
        assert conn.execute('SELECT cursor FROM source_cursors').fetchone()[0] == before
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_service_drains_pages_before_publishing_current_status(configs):
    tasks, messages = configs
    with storage._connect(tasks, write=True) as conn:
        for index in range(1001):
            storage._save_task(conn, dict(task_id='t1', priority='P2', status='completed' if index == 1000 else 'open',
                title='Report', event='Submit report', sources=[]))
    assert sync(messages)['imported_events'] == 1001
    assert preview(messages)['tasks'][0]['status'] == 'completed'


def test_message_console_updates_formal_task_even_without_feedback_queue(configs):
    tasks, messages = configs
    save(tasks, 't1')
    sync(messages)
    result = set_task_state(messages, 't1', 'complete')
    assert result['status'] == 'completed'
    assert service.get_task(tasks, 't1')['status'] == 'completed'
    set_task_state(messages, 't1', 'reopen')
    assert service.get_task(tasks, 't1')['status'] == 'open'


def test_materials_are_resolved_by_task_service_once_and_failure_is_local(configs, monkeypatch):
    tasks, messages = configs
    source = {'message_key': 'm1', 'attachments': [{'type': 'file', 'name': 'a.pdf'}]}
    item = {'message_key': 'm1', 'attachment_index': 0, 'attachment': source['attachments'][0]}
    task = save(tasks, 't1', sources=[source], materials=[item], attachment_evidence=[item])
    calls = []
    def resolve(config, task_id, material_id):
        calls.append((task_id, material_id))
        raise ValueError('task_material_source_missing')
    monkeypatch.setattr(service, 'get_material', resolve)
    package = core._package(task, messages, prepare=True)
    assert len(calls) == 1
    assert package['resources'][0]['status'] == 'unavailable'


def test_legacy_export_import_can_switch_to_service_without_reemitting(configs):
    tasks, messages = configs
    save(tasks, 't1')
    with storage._connect(tasks, write=True) as conn:
        storage._export(tasks, conn)
    source = messages['input'].pop('processor_config')
    assert sync(messages)['imported_events'] == 1
    set_task_state(messages, 't1', 'complete')
    assert preview(messages)['tasks'][0]['status'] == 'completed'
    messages['input']['processor_config'] = source
    assert preview(messages)['tasks'][0]['status'] == 'open'
    assert sync(messages)['imported_events'] == 0
    assert preview(messages)['total_tasks'] == 1
    with core._connect(messages) as conn:
        assert conn.execute('SELECT completed FROM local_state').fetchone()[0] == 0
