"""Shared validation of the existing mapped Pi submission."""
import datetime as dt
from zoneinfo import ZoneInfo

from common.codec import digest


def _text(value, name, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 20000:
        raise ValueError(f'Model task {name} must be nonempty text')
    return value.strip()


def _validate_response(response, group, existing, timezone):
    if not isinstance(response, dict) or not isinstance(response.get('tasks'), list) or len(response['tasks']) > 100:
        raise ValueError('Model response must contain at most 100 tasks')
    allowed = {event['message_key'] for event in group['messages']}
    primary = set(group['primary_message_keys'])
    existing_ids = {task['task_id'] for task in existing}
    tasks, assigned, signatures = [], set(), set()
    for item in response['tasks']:
        if not isinstance(item, dict):
            raise ValueError('Model task must be an object')
        task = {name: _text(item.get(name), name) for name in ('title', 'event', 'priority_reason')}
        steps = item.get('action_steps', [])
        if not isinstance(steps, list) or len(steps) > 50 or any(not isinstance(step, str) or not step.strip() or len(step) > 2000 for step in steps):
            raise ValueError('Model task action_steps must contain at most 50 nonempty strings of at most 2000 characters')
        task['action_steps'] = [step.strip() for step in steps]
        if not isinstance(item.get('priority'), str) or item['priority'] not in {'P0', 'P1', 'P2', 'P3', 'P4'} or not isinstance(item.get('status'), str) or item['status'] not in {'open', 'cancelled'}:
            raise ValueError('Model task priority or status is invalid')
        task.update(priority=item['priority'], status=item['status'])
        keys = item.get('source_message_keys')
        if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or key not in allowed for key in keys):
            raise ValueError('Model task references missing or fabricated source messages')
        task['source_message_keys'] = sorted(set(keys))
        identifier = item.get('task_id')
        if identifier is not None and (not isinstance(identifier, str) or identifier not in existing_ids or identifier in assigned):
            raise ValueError('Model task_id must be a unique supplied existing task ID or null')
        if identifier is None and not primary.intersection(keys):
            raise ValueError('New tasks cannot be extracted solely from historical context')
        task['task_id'] = identifier
        if identifier:
            assigned.add(identifier)
        time = item.get('time')
        if not isinstance(time, dict):
            raise ValueError('Model task time must be an object')
        task['time'] = {'timezone': timezone, 'text': _text(time.get('text'), 'time.text', nullable=True)}
        for field in ('start', 'deadline'):
            value = _text(time.get(field), 'time.' + field, nullable=True)
            if value:
                try:
                    if len(value) == 10:
                        dt.date.fromisoformat(value)
                    elif dt.datetime.fromisoformat(value).tzinfo is None:
                        raise ValueError('missing offset')
                except ValueError:
                    raise ValueError('Model times must be ISO dates or timestamps with timezone offsets') from None
            task['time'][field] = value
        people = item.get('people')
        if not isinstance(people, list) or len(people) > 100 or any(not isinstance(p, dict) for p in people):
            raise ValueError('Model task people must be an array')
        task['people'] = [{name: _text(person.get(name), 'people.' + name) for name in ('name', 'role')} for person in people]
        task['location'] = _text(item.get('location'), 'location', nullable=True)
        if type(item.get('needs_review', False)) is not bool or not isinstance(item.get('review_reasons', []), list):
            raise ValueError('Legacy task explanation fields are invalid')
        notes = item.get('notes', [])
        legacy_notes = item.get('review_reasons', [])
        if not isinstance(notes, list) or len(notes) + len(legacy_notes) > 100:
            raise ValueError('Model task notes must be an array of at most 100 strings')
        task['notes'] = list(dict.fromkeys(_text(note, 'note') for note in [*notes, *legacy_notes]))
        # Legacy fields remain readable; uncertainty is explanatory text, not an approval state.
        task['review_reasons'] = list(task['notes'])
        task['needs_review'] = False
        task['attachment_evidence'] = item.get('attachment_evidence', [])
        materials = item.get('materials', [])
        messages = {event['message_key']: event for event in group['messages']}
        if not isinstance(materials, list) or len(materials) > 1000:
            raise ValueError('Task materials must be a bounded array of mapped attachments')
        task['materials'] = []
        for material in materials:
            if (not isinstance(material, dict) or set(material) != {'message_key', 'attachment_index', 'attachment'}
                    or not isinstance(material['message_key'], str) or material['message_key'] not in task['source_message_keys']
                    or type(material['attachment_index']) is not int
                    or not 0 <= material['attachment_index'] < len(messages[material['message_key']]['message'].get('attachments', []))
                    or not isinstance(material['attachment'], dict)):
                raise ValueError('Task materials must map to cited source attachment indices')
            task['materials'].append(material)
        signature = digest([task['title'], task['event'], task['source_message_keys']])
        if signature in signatures:
            raise ValueError('Model returned duplicate tasks in one response')
        signatures.add(signature)
        tasks.append(task)
    return tasks


def _source(config, event):
    message = event['message']
    return {'message_key': event['message_key'], 'event_id': event['event_id'], 'revision': event['revision'],
            'collector_module': message['platform'],
            'collector_description': config['source_descriptions'].get(message['platform'], message['platform'] + ' local collector'),
            'account_id': message['account_id'], 'conversation_id': message['conversation_id'],
            'conversation_type': message.get('conversation_type'), 'message_id': message['message_id'],
            'sender_id': message['sender_id'], 'sent_at': message['sent_at'],
            'sent_at_local': dt.datetime.fromtimestamp(message['sent_at'], ZoneInfo(config['context']['timezone'])).isoformat(),
            'collected_at': event.get('collected_at'), 'text': message['text'],
            'attachments': message.get('attachments', []), 'source': message.get('source', {}),
            'identity': message.get('identity', {}), 'sender': message.get('sender'),
            'kind': message.get('kind'), 'mentions_all': message.get('mentions_all', False),
            'context_notes': message.get('context_notes', []),
            'matched_rules': event.get('matched_rules', []), 'window': event.get('window')}

