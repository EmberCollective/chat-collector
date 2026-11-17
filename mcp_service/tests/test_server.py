import asyncio
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mcp_service import build_server
from messaging import load_config as load_messaging, sync
from messaging.management import read_settings


@pytest.fixture
def settings(tmp_path):
    (tmp_path / 'batches').mkdir()
    (tmp_path / 'todos').mkdir()
    (tmp_path / 'rules.md').write_text('Only explicit tasks.', encoding='utf-8')
    (tmp_path / 'processor.toml').write_text('''[input]
batches_dir = "batches"
[storage]
state_file = "processor.db"
output_dir = "todos"
[rules]
file = "rules.md"
[model]
base_url = "http://127.0.0.1:9/v1"
model = "test-only"
api_key_env = ""
api_key_file = ""
[context]
grouping = "time"
timezone = "Asia/Shanghai"
[agent]
batching = "single_conversation"
''', encoding='utf-8')
    (tmp_path / 'messaging.toml').write_text('''enabled = true
[input]
processor_config = "processor.toml"
batches_dir = "todos"
[storage]
state_file = "messaging.db"
assets_dir = "messaging-assets"
[feedback]
enabled = false
[reminders]
P0 = 3600
P1 = 14400
P2 = 86400
P3 = 604800
P4 = 0
[deadline_reminders]
enabled = true
timezone = "Asia/Shanghai"
P0 = [86400, 21600, 7200, 1800, 0]
P1 = [21600, 7200, 1800, 0]
P2 = [7200, 1800, 0]
P3 = [1800, 0]
P4 = [0]
[initial_notifications]
P0 = true
P1 = true
P2 = true
P3 = true
P4 = true
[daily_digest]
enabled = true
time = "08:00"
timezone = "Asia/Shanghai"
catch_up_minutes = 360
[[targets]]
id = "notify"
kind = "feishu"
enabled = true
''', encoding='utf-8')
    return {'root': tmp_path, 'processor': tmp_path / 'processor.toml', 'messaging': tmp_path / 'messaging.toml'}


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    structured = getattr(result, 'structuredContent', None) or getattr(result, 'structured_content', None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {'result'}:
            return structured['result']
        return structured
    return json.loads(result.content[0].text)


def test_server_reports_project_version(settings):
    from common.version import project_version
    assert build_server(settings['root'], 'processor.toml', 'messaging.toml').version == project_version()


def test_tool_list_and_audited_task_lifecycle(settings):
    server = build_server(settings['root'], 'processor.toml', 'messaging.toml')
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {
        'tasks_search', 'tasks_get', 'tasks_create', 'tasks_set_status', 'tasks_set_priority',
        'reminders_schedule', 'daily_digest_preview', 'notifications_configure',
    }
    deadline = (dt.datetime.now(ZoneInfo('Asia/Shanghai')) + dt.timedelta(days=2)).replace(microsecond=0).isoformat()
    created = _call(server, 'tasks_create', {
        'tasks': [{'title': '提交报告', 'event': '把报告发给老师', 'priority': 'P1',
                   'deadline': deadline, 'action_steps': ['完成正文', '发送报告']}],
        'request_id': 'mcp-create-1',
        'actor_platform': 'mcp',
        'actor_user_id': 'tester',
        'actor_source': 'mcp-test',
    })
    task_id = created['tasks'][0]['task_id']
    assert created['tasks'][0]['priority'] == 'P1'
    assert 'sources' not in created['tasks'][0]

    found = _call(server, 'tasks_search', {'query': '报告', 'statuses': ['open']})
    assert [task['task_id'] for task in found['tasks']] == [task_id]
    detail = _call(server, 'tasks_get', {'task_id': task_id})
    assert detail['revision'] == 1 and detail['time']['deadline'] == deadline

    priority = _call(server, 'tasks_set_priority', {
        'task_id': task_id, 'priority': 'P0', 'request_id': 'mcp-priority-1',
        'actor_platform': 'mcp', 'actor_user_id': 'tester', 'actor_source': 'mcp-test',
        'expected_revision': 1,
    })
    assert priority['priority'] == 'P0' and priority['previous_priority'] == 'P1'

    completed = _call(server, 'tasks_set_status', {
        'task_id': task_id, 'status': 'completed', 'request_id': 'mcp-status-1',
        'actor_platform': 'mcp', 'actor_user_id': 'tester', 'actor_source': 'mcp-test',
        'expected_revision': priority['revision'],
    })
    assert completed['status'] == 'completed'


def test_reminder_countdown_digest_window_and_guarded_configuration(settings):
    server = build_server(settings['root'], 'processor.toml', 'messaging.toml')
    future = (dt.datetime.now(ZoneInfo('Asia/Shanghai')) + dt.timedelta(days=2)).replace(microsecond=0).isoformat()
    _call(server, 'tasks_create', {
        'tasks': [{'title': '准备答辩', 'event': '准备答辩材料', 'priority': 'P1', 'deadline': future}],
        'request_id': 'mcp-create-reminder', 'actor_platform': 'mcp',
        'actor_user_id': 'tester', 'actor_source': 'mcp-test',
    })
    assert sync(load_messaging(settings['messaging']))['imported_events'] == 1

    schedule = _call(server, 'reminders_schedule', {})
    item = next(task for task in schedule['tasks'] if task['title'] == '准备答辩')
    assert len(item['nodes']) == 4
    assert item['next_reminder_in_seconds'] >= 0
    assert item['nodes'][0]['due_at_iso'] and schedule['next_reminder']['in_seconds'] >= 0
    assert schedule['settings_version']

    digest = _call(server, 'daily_digest_preview', {})
    assert digest['settings']['time'] == '08:00'
    assert digest['settings']['catch_up_minutes'] == 360
    assert digest['window_state'] in {'upcoming', 'delivery_window', 'passed'}
    assert digest['slot_iso'] and digest['expires_at_iso']

    version = read_settings(settings['messaging'])['version']
    configured = _call(server, 'notifications_configure', {
        'expected_version': version,
        'deadline_reminders': {'P1': [10800, 3600, 0]},
        'daily_digest': {'time': '09:15', 'enabled': False},
    })
    assert configured['deadline_reminders']['P1'] == [10800, 3600, 0]
    assert configured['daily_digest']['time'] == '09:15'
    assert configured['version'] != version
