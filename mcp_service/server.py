from __future__ import annotations

import argparse
import datetime as dt
from functools import wraps
from pathlib import Path
import sys
from typing import Any, Literal
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import messaging
import processor
from common.version import project_version
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from messaging import planning
from messaging.management import read_settings, save_settings
from pydantic import BaseModel, ConfigDict, Field


READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
CONFIG_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


def _expected_errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FileExistsError as exc:
            raise ToolError(str(exc)) from exc
        except (processor.TaskStatusBusy, processor.TaskStatusExportPending) as exc:
            raise ToolError(str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise ToolError(str(exc)) from exc
        except OSError as exc:
            raise ToolError('local_data_unavailable') from exc
    return wrapped


class PersonInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    role: str


class TaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str
    event: str | None = None
    action_steps: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    priority: Literal['P0', 'P1', 'P2', 'P3', 'P4'] = 'P2'
    status: Literal['open', 'cancelled'] = 'open'
    deadline: str | None = None
    start: str | None = None
    time_text: str | None = None
    people: list[PersonInput] = Field(default_factory=list)
    location: str | None = None


def _path(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    """Keep task data useful to MCP clients while omitting chat bodies and local paths."""
    sources = task.get('sources') or []
    platforms = sorted({source.get('platform') or source.get('collector_module')
                        for source in sources if source.get('platform') or source.get('collector_module')})
    return {
        'task_id': task.get('task_id'),
        'revision': task.get('revision'),
        'title': task.get('title'),
        'event': task.get('event'),
        'action_steps': task.get('action_steps', []),
        'notes': task.get('notes', []),
        'priority': task.get('priority'),
        'priority_reason': task.get('priority_reason'),
        'status': task.get('status'),
        'time': task.get('time'),
        'people': task.get('people', []),
        'location': task.get('location'),
        'needs_review': task.get('needs_review', False),
        'updated_at': task.get('updated_at'),
        'source_count': len(sources),
        'source_platforms': platforms,
        'material_count': len(task.get('materials', [])),
    }


def _manual_task(task: TaskInput, timezone: str) -> dict[str, Any]:
    task = task.model_dump()
    return {
        'task_id': None,
        'title': task['title'],
        'event': task['event'] or task['title'],
        'action_steps': task['action_steps'],
        'notes': task['notes'],
        'priority': task['priority'],
        'priority_reason': 'Manual task created through MCP.',
        'status': task['status'],
        'time': {
            'timezone': timezone,
            'start': task['start'],
            'deadline': task['deadline'],
            'text': task['time_text'],
        },
        'people': task['people'],
        'location': task['location'],
        'needs_review': False,
        'review_reasons': [],
        'attachment_evidence': [],
        'materials': [],
    }


def _iso(timestamp: float | None, zone: ZoneInfo) -> str | None:
    return dt.datetime.fromtimestamp(timestamp, zone).isoformat() if timestamp is not None else None


def _remaining(timestamp: float | None, now: float) -> int | None:
    return max(0, int(round(timestamp - now))) if timestamp is not None else None


def _decorate_schedule(result: dict[str, Any], settings_version: str) -> dict[str, Any]:
    now = float(result['generated_at'])
    zone = ZoneInfo(result['rules']['deadline_reminders']['timezone'])
    candidates = []
    for task in result['tasks']:
        next_at = task.get('next_at')
        task['next_reminder_at'] = _iso(next_at, zone)
        task['next_reminder_in_seconds'] = _remaining(next_at, now)
        for node in task.get('nodes', []):
            node['due_at_iso'] = _iso(node.get('due_at'), zone)
            node['due_in_seconds'] = _remaining(node.get('due_at'), now)
        for plan in task.get('plans', []):
            plan['due_at_iso'] = _iso(plan.get('due_at'), zone)
            plan['due_in_seconds'] = _remaining(plan.get('due_at'), now)
        if next_at is None:
            continue
        plan = next((item for item in task.get('plans', []) if item.get('due_at') == next_at), {})
        candidates.append({
            'task_id': task['task_id'],
            'title': task['title'],
            'target_id': plan.get('target_id'),
            'at': _iso(next_at, zone),
            'in_seconds': _remaining(next_at, now),
            'state': plan.get('state'),
            'reason': plan.get('reason'),
        })
    result['next_reminder'] = min(candidates, key=lambda item: item['at']) if candidates else None
    result['next_reminder_scope'] = 'returned_tasks'
    result['settings_version'] = settings_version
    return result


def _decorate_digest(result: dict[str, Any], settings_version: str, settings: dict[str, Any]) -> dict[str, Any]:
    now = float(result['generated_at'])
    zone = ZoneInfo(result['timezone'])
    slot = float(result['slot'])
    expires = float(result['expires_at'])
    result['slot_iso'] = _iso(slot, zone)
    result['expires_at_iso'] = _iso(expires, zone)
    result['seconds_until_slot'] = int(round(slot - now))
    result['seconds_until_expiry'] = max(0, int(round(expires - now)))
    result['window_state'] = 'upcoming' if now < slot else 'delivery_window' if now < expires else 'passed'
    result['settings'] = {
        'enabled': bool(result['enabled']),
        'time': settings['time'],
        'timezone': settings['timezone'],
        'catch_up_minutes': settings['catch_up_minutes'],
    }
    result['settings_version'] = settings_version
    return result


def build_server(root: str | Path | None = None,
                 processor_config: str = 'config/processor.toml',
                 messaging_config: str = 'config/messaging.toml') -> MCPServer:
    root = Path(root).resolve() if root else ROOT
    processor_path = _path(root, processor_config)
    messaging_path = _path(root, messaging_config)
    server = MCPServer(
        name='chat-collector',
        title='Chat Collector Todos',
        description='Local todo access, audited edits, deadline reminders and daily digest previews.',
        version=project_version(),
        instructions=(
            'Use reminders_schedule for the next deadline reminder and countdown. '
            'Use daily_digest_preview for digest content and delivery-window details. '
            'Mutating tools require a unique request_id and the current revision when available.'
        ),
    )

    @server.tool(title='Search todos', annotations=READ)
    @_expected_errors
    def tasks_search(
        query: str | None = None,
        platforms: list[str] | None = None,
        people: list[str] | None = None,
        person_role: str | None = None,
        statuses: list[Literal['open', 'completed', 'cancelled', 'superseded']] | None = None,
        priorities: list[Literal['P0', 'P1', 'P2', 'P3', 'P4']] | None = None,
        needs_review: bool | None = None,
        time_field: Literal['deadline', 'start', 'updated_at', 'source_time'] = 'deadline',
        time_from: str | None = None,
        time_to: str | None = None,
        sort_by: Literal['deadline', 'start', 'updated_at'] = 'updated_at',
        order: Literal['asc', 'desc'] = 'desc',
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search persisted todos without reading source chat databases."""
        config = processor.load_config(processor_path)
        result = processor.search_tasks(
            config, query=query, platforms=platforms, people=people, person_role=person_role,
            statuses=statuses, priorities=priorities, needs_review=needs_review,
            time_field=time_field, time_from=time_from, time_to=time_to,
            sort_by=sort_by, order=order, limit=limit, offset=offset,
        )
        return {**result, 'tasks': [_public_task(task) for task in result['tasks']]}

    @server.tool(title='Get todo', annotations=READ)
    @_expected_errors
    def tasks_get(task_id: str) -> dict[str, Any]:
        """Get one todo, including its current revision for safe edits."""
        return _public_task(processor.get_task(processor.load_config(processor_path), task_id))

    @server.tool(title='Create todos', annotations=WRITE)
    @_expected_errors
    def tasks_create(
        tasks: list[TaskInput],
        request_id: str,
        actor_platform: str,
        actor_user_id: str,
        actor_source: str = 'mcp',
        account_id: str = 'mcp',
        conversation_id: str = 'mcp',
    ) -> dict[str, Any]:
        """Create manual todos through the existing audited task service."""
        config = processor.load_config(processor_path)
        actor = {'platform': actor_platform, 'user_id': actor_user_id, 'source': actor_source}
        origin = {'kind': 'manual', 'platform': actor_platform,
                  'account_id': account_id, 'conversation_id': conversation_id}
        result = processor.create_tasks(
            config,
            [_manual_task(task, config['context']['timezone']) for task in tasks],
            request_id=request_id, actor=actor, origin=origin,
        )
        return {**result, 'tasks': [_public_task(task) for task in result['tasks']]}

    @server.tool(title='Set todo status', annotations=WRITE)
    @_expected_errors
    def tasks_set_status(
        task_id: str,
        status: Literal['completed', 'open', 'cancelled'],
        request_id: str,
        actor_platform: str,
        actor_user_id: str,
        actor_source: str = 'mcp',
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Complete, reopen or cancel a todo using the existing audited write path."""
        config = processor.load_config(processor_path)
        return processor.update_task_status(
            config, task_id, status, request_id=request_id,
            actor={'platform': actor_platform, 'user_id': actor_user_id, 'source': actor_source},
            expected_revision=expected_revision,
        )

    @server.tool(title='Set todo priority', annotations=WRITE)
    @_expected_errors
    def tasks_set_priority(
        task_id: str,
        priority: Literal['P0', 'P1', 'P2', 'P3', 'P4'],
        request_id: str,
        actor_platform: str,
        actor_user_id: str,
        actor_source: str = 'mcp',
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Change priority; subsequent deadline reminder nodes use the new priority."""
        config = processor.load_config(processor_path)
        return processor.update_task_priority(
            config, task_id, priority, request_id=request_id,
            actor={'platform': actor_platform, 'user_id': actor_user_id, 'source': actor_source},
            expected_revision=expected_revision,
        )

    @server.tool(title='Deadline reminder schedule', annotations=READ)
    @_expected_errors
    def reminders_schedule(
        search: str = '',
        state: Literal['all', 'scheduled', 'attention', 'ended'] = 'all',
        target_id: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Show deadline nodes, next reminder countdown, target plans and blockers."""
        config = messaging.load_config(messaging_path)
        settings_version = read_settings(messaging_path)['version']
        result = planning.schedule(
            config, limit=limit, offset=offset, search=search, state=state,
            target_id=target_id, task_id=task_id,
        )
        return _decorate_schedule(result, settings_version)

    @server.tool(title='Daily digest preview', annotations=READ)
    @_expected_errors
    def daily_digest_preview(day: str | None = None, target_id: str | None = None) -> dict[str, Any]:
        """Preview digest items, exclusions, delivery state and the current digest window."""
        config = messaging.load_config(messaging_path)
        settings_version = read_settings(messaging_path)['version']
        result = planning.digest_preview(config, day=day, target_id=target_id)
        return _decorate_digest(result, settings_version, config['daily_digest'])

    @server.tool(title='Configure reminders and digest', annotations=CONFIG_WRITE)
    @_expected_errors
    def notifications_configure(
        expected_version: str,
        deadline_reminders: dict[str, Any] | None = None,
        daily_digest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update only deadline-reminder and daily-digest settings after a version check."""
        payload: dict[str, Any] = {}
        if deadline_reminders is not None:
            payload['deadline_reminders'] = deadline_reminders
        if daily_digest is not None:
            payload['daily_digest'] = daily_digest
        if not payload:
            raise ValueError('no_notification_settings_supplied')
        saved = save_settings(messaging_path, payload, expected_version)
        return {
            'version': saved['version'],
            'deadline_reminders': saved['deadline_reminders'],
            'daily_digest': saved['daily_digest'],
        }

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description='Run the Chat Collector MCP server over stdio.')
    parser.add_argument('--root', default=str(ROOT), help='Chat Collector repository root')
    parser.add_argument('--processor-config', default='config/processor.toml')
    parser.add_argument('--messaging-config', default='config/messaging.toml')
    args = parser.parse_args()
    build_server(args.root, args.processor_config, args.messaging_config).run('stdio')


if __name__ == '__main__':
    main()
