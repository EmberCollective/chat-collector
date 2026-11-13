"""Public v1 message contract and immutable inbox publication; no model or platform I/O."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile


NOTE_KINDS = {'conversation_topic', 'message_state', 'audience', 'thread'}


def validate_context_notes(notes):
    if not isinstance(notes, list) or len(notes) > 8:
        raise ValueError('context_notes must contain at most 8 facts')
    for note in notes:
        if (not isinstance(note, dict) or set(note) != {'kind', 'text'}
                or not isinstance(note['kind'], str) or note['kind'] not in NOTE_KINDS
                or not isinstance(note['text'], str) or not 1 <= len(note['text'].strip()) <= 500):
            raise ValueError('context_notes require a supported kind and 1-500 characters of text')
    return notes


def validate_event(event):
    if (not isinstance(event, dict) or type(event.get('schema_version')) is not int
            or event['schema_version'] != 1 or event.get('operation') != 'upsert'):
        raise ValueError('Unsupported collector event schema')
    for key in ('event_id', 'message_key'):
        if not isinstance(event.get(key), str) or not event[key]:
            raise ValueError(f'Collector event lacks {key}')
    if type(event.get('revision')) is not int or event['revision'] < 1:
        raise ValueError('Collector revision must be a positive integer')
    message = event.get('message')
    if not isinstance(message, dict):
        raise ValueError('Collector event lacks message')
    for key in ('platform', 'account_id', 'conversation_id', 'message_id'):
        if not isinstance(message.get(key), str) or not message[key]:
            raise ValueError(f'Collector message lacks {key}')
    if not isinstance(message.get('sender_id'), str):
        raise ValueError('Collector sender_id must be text; empty means unknown')
    if type(message.get('sent_at')) is not int or not isinstance(message.get('text'), str):
        raise ValueError('Collector message needs a Unix timestamp and text')
    for name, rows in [('attachments', message.get('attachments', [])), ('matched_rules', event.get('matched_rules', []))]:
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f'Collector {name} must contain objects')
    for key in ('source', 'metadata'):
        if key in message and not isinstance(message[key], dict):
            raise ValueError(f'Collector {key} must be an object')
    for attachment in message.get('attachments', []):
        for key in ('type', 'name', 'archive_path', 'sha256'):
            if attachment.get(key) is not None and not isinstance(attachment[key], str):
                raise ValueError(f'Collector attachment.{key} must be text')
    if 'window' in event:
        window = event['window']
        if (not isinstance(window, dict) or any(type(window.get(k)) is not int
                for k in ('start_inclusive', 'end_exclusive'))
                or window['end_exclusive'] <= window['start_inclusive']):
            raise ValueError('Collector window must be a nonempty half-open interval')
    for key in ('sender', 'kind', 'conversation_type', 'reply_sender_id'):
        if key in message and not isinstance(message[key], str):
            raise ValueError(f'Collector {key} must be text')
    if 'mentions_all' in message and type(message['mentions_all']) is not bool:
        raise ValueError('Collector mentions_all must be a boolean')
    mentioned = message.get('mentioned_user_ids', [])
    if not isinstance(mentioned, list) or any(not isinstance(p, str) or not p for p in mentioned):
        raise ValueError('Collector mentioned_user_ids must contain nonempty IDs')
    if 'context_notes' in message:
        validate_context_notes(message['context_notes'])
    if 'extensions' in message:
        extensions = message['extensions']
        if not isinstance(extensions, dict) or any(not isinstance(v, dict) for v in extensions.values()):
            raise ValueError('Collector extensions must be namespaced objects')
    if 'identity' in message:
        identity = message['identity']
        if not isinstance(identity, dict):
            raise ValueError('Collector identity must be an object')
        allowed = {message['sender_id'], *mentioned}
        if message.get('reply_sender_id'):
            allowed.add(message['reply_sender_id'])
        if message.get('conversation_type') == 'direct':
            allowed.add(message['conversation_id'])
        people = identity.get('people', [])
        if not isinstance(people, list):
            raise ValueError('Collector identity.people must be an array')
        objects = []
        for field, expected in [('account', message['account_id']), ('conversation', message['conversation_id'])]:
            if field in identity:
                value = identity[field]
                if not isinstance(value, dict) or value.get('id') != expected:
                    raise ValueError(f'Collector identity.{field} does not match message')
                objects.append(value)
        for person in people:
            if (not isinstance(person, dict) or not isinstance(person.get('id'), str)
                    or not person['id'] or person['id'] not in allowed):
                raise ValueError('Collector identity.people exceeds selected message scope')
            if 'is_self' in person and (type(person['is_self']) is not bool
                    or person['is_self'] != (person['id'] == message['account_id'])):
                raise ValueError('Collector is_self conflicts with account identity')
            objects.append(person)
        for value in objects:
            for field in ('nickname', 'remark', 'group_nickname', 'display_name', 'label', 'name'):
                if value.get(field) is not None and not isinstance(value[field], str):
                    raise ValueError(f'Collector identity.{field} must be text or null')
    return event


def ingest_batch(events, inbox):
    """Validate and publish v1 events; repeat identical batches safely, never run AI."""
    rows = []
    seen = {}
    for event in events:
        validate_event(event)
        body = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        for identity in [('event', event['event_id']), ('revision', event['message_key'], event['revision'])]:
            if identity in seen and seen[identity] != body:
                raise ValueError('Conflicting events within one batch')
            seen[identity] = body
        rows.append(body)
    if not rows:
        return None
    raw = ('\n'.join(dict.fromkeys(rows)) + '\n').encode('utf-8')
    directory = Path(inbox).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / ('provider-' + hashlib.sha256(raw).hexdigest() + '.jsonl')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A hard link publishes a complete file without replacing another writer's file.
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != raw:
                raise ValueError('Immutable provider batch was modified') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination
