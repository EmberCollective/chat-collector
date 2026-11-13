"""Deterministic delivery adapters; no model, OAuth implementation or collector access."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx


class DeliveryError(RuntimeError):
    def __init__(self, code, *, uncertain=False):
        super().__init__(code)
        self.code, self.uncertain = code, uncertain


class DeliveryRejected(DeliveryError):
    pass


class DeliveryUncertain(DeliveryError):
    def __init__(self, code):
        super().__init__(code, uncertain=True)


def _secret(target, field, config, *, required=True):
    name = target.get(field, '')
    value = os.environ.get(name, '') if name else ''
    if not value and name and config.get('env_file'):
        from dotenv import dotenv_values
        value = dotenv_values(config['env_file'], interpolate=False).get(name) or ''
    if not isinstance(value, str) or not value.strip():
        if required:
            raise DeliveryRejected('credential_not_configured')
        return ''
    return value.strip()


def _url(value):
    try:
        parsed = urlsplit(value)
        return (isinstance(value, str) and parsed.scheme in {'https', 'http'}
                and bool(parsed.hostname) and not parsed.username and not parsed.password
                and not any(ord(c) < 32 for c in value))
    except (ValueError, TypeError):
        return False


def _text_fields(value, names):
    if not isinstance(value, dict):
        raise DeliveryRejected('invalid_delivery_payload')
    fields = {k: value[k] for k in names if k in value}
    if any(v is not None and not isinstance(v, str) for v in fields.values()):
        raise DeliveryRejected('invalid_delivery_payload')
    return fields


def _text_list(value):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DeliveryRejected('invalid_delivery_payload')
    return list(value)


def public_envelope(envelope):
    """Allowlist outbound fields so future local metadata cannot leak by accident."""
    task = envelope['task']
    header = _text_fields(envelope, ('delivery_id', 'kind'))
    result = _text_fields(task, ('task_id', 'title', 'event', 'priority', 'priority_reason', 'status', 'location'))
    if type(task.get('revision')) is not int or task['revision'] < 1:
        raise DeliveryRejected('invalid_delivery_payload')
    result['revision'] = task['revision']
    if 'needs_review' in task:
        if type(task['needs_review']) is not bool:
            raise DeliveryRejected('invalid_delivery_payload')
        result['needs_review'] = task['needs_review']
    result['time'] = _text_fields(task.get('time', {}), ('start', 'deadline', 'timezone', 'text'))
    if any(not isinstance(task.get(field, []), list) for field in ('people', 'sources', 'resources')):
        raise DeliveryRejected('invalid_delivery_payload')
    result['people'] = [_text_fields(p, ('name', 'role')) for p in task.get('people', [])]
    for field in ('review_reasons', 'action_steps'):
        if field in task:
            result[field] = _text_list(task[field])
    result['sources'] = [_text_fields(s, ('ref', 'platform', 'conversation', 'sender', 'time'))
                         for s in task.get('sources', [])]
    result['resources'] = []
    for resource in task.get('resources', []):
        safe = _text_fields(resource, ('id', 'name', 'type', 'status', 'sha256', 'purpose', 'role', 'note'))
        if 'size_bytes' in resource:
            if type(resource['size_bytes']) is not int or resource['size_bytes'] < 0:
                raise DeliveryRejected('invalid_delivery_payload')
            safe['size_bytes'] = resource['size_bytes']
        for field in ('source_refs', 'warnings'):
            if field in resource:
                safe[field] = _text_list(resource[field])
        if _url(resource.get('url')):
            safe['url'] = resource['url']
        result['resources'].append(safe)
    return {'schema_version': 1, **header, 'task': result}


def _instructions(task, *, material_destination='见材料附件'):
    lines = [str(task.get('event') or task['title'])]
    if task.get('action_steps'):
        lines += ['你需要做：', *[f'{i}. {step}' for i, step in enumerate(task['action_steps'], 1)]]
    for name, value in [('开始', task.get('time', {}).get('start')),
                        ('截止', task.get('time', {}).get('deadline')), ('地点', task.get('location'))]:
        if value:
            lines.append(f'{name}：{value}')
    if task.get('people'):
        lines.append('参与人员：' + '；'.join(f"{p['name']}（{p['role']}）" for p in task['people']))
    for r in task.get('resources', []):
        destination = r.get('url') if _url(r.get('url')) else material_destination if r.get('local_path') else '原件尚不可用'
        label = '完成成果' if r.get('role') == 'result' else '执行材料'
        lines.append(f"{label}：{r.get('name', r['id'])} — {destination}" + (f"；用途：{r['purpose']}" if r.get('purpose') else ''))
        if r.get('note'):
            lines.append('成果说明：' + r['note'])
    for source in task.get('sources', []):
        lines.append('来源：' + ' · '.join(str(source[k]) for k in ('platform', 'conversation', 'sender', 'time') if source.get(k)))
    if task.get('needs_review'):
        lines.append('需要确认：' + '；'.join(task.get('review_reasons', [])))
    return '\n'.join(lines)


def _post(url, payload, *, headers, timeout):
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=timeout, follow_redirects=False)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise DeliveryRejected('connection_failed') from None
    except httpx.HTTPError:
        raise DeliveryUncertain('delivery_outcome_unknown') from None
    if response.status_code >= 500:
        raise DeliveryUncertain('remote_server_error')
    if not 200 <= response.status_code < 300:
        raise DeliveryRejected('remote_request_rejected')
    try:
        return response.json()
    except ValueError:
        raise DeliveryUncertain('invalid_delivery_receipt') from None


def _webhook(target, envelope, config, previous):
    url = _secret(target, 'url_env', config)
    if not _url(url):
        raise DeliveryRejected('invalid_webhook_url')
    payload = public_envelope(envelope)
    if previous and previous.get('remote_id'):
        payload['remote_id'] = previous['remote_id']
    headers = {'Idempotency-Key': envelope['delivery_id']}
    token = _secret(target, 'token_env', config, required=False)
    if token:
        headers['Authorization'] = 'Bearer ' + token
    result = _post(url, payload, headers=headers, timeout=target.get('timeout_seconds', 30))
    if not isinstance(result, dict) or result.get('accepted') is not True or result.get('delivery_id') != envelope['delivery_id']:
        raise DeliveryUncertain('webhook_acknowledgement_missing')
    receipt = {'delivery_id': envelope['delivery_id']}
    for key in ('remote_id', 'url'):
        if isinstance(result.get(key), str) and (key != 'url' or _url(result[key])):
            receipt[key] = result[key]
    return receipt


def _feishu(target, envelope, config):
    url = _secret(target, 'url_env', config)
    if not _url(url) or urlsplit(url).hostname not in {'open.feishu.cn', 'open.larksuite.com'}:
        raise DeliveryRejected('invalid_feishu_webhook_url')
    task = envelope['task']
    text = _instructions(task, material_destination='本地已归档，机器人未上传')
    local_only = [r['name'] for r in task.get('resources', []) if r.get('local_path') and not _url(r.get('url'))]
    if local_only:
        text += '\n仅本机已保存（机器人消息不支持直接上传文件）：' + '；'.join(local_only)
    payload = {'msg_type': 'text', 'content': {'text': f"[{task['priority']}] {task['title']}\n状态：{task['status']}\n{text}"}}
    secret = _secret(target, 'secret_env', config, required=False)
    if secret:
        stamp = str(int(time.time()))
        payload.update(timestamp=stamp, sign=base64.b64encode(hmac.new(f'{stamp}\n{secret}'.encode(), b'', hashlib.sha256).digest()).decode())
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 20000:
        raise DeliveryRejected('feishu_message_too_large')
    result = _post(url, payload, headers={}, timeout=target.get('timeout_seconds', 30))
    if not isinstance(result, dict) or result.get('code', result.get('StatusCode')) != 0:
        raise DeliveryRejected('feishu_message_rejected')
    return {'delivery_id': envelope['delivery_id'], 'warnings': ['local_materials_not_uploaded'] if local_only else []}


def _rich_text(text):
    if len(text) > 200000:
        raise DeliveryRejected('notion_text_too_large')
    return [{'type': 'text', 'text': {'content': text[i:i + 2000]}} for i in range(0, len(text), 2000)]


def _notion_client(token, timeout):
    try:
        from notion_client import Client
    except ImportError:
        raise DeliveryRejected('notion_sdk_not_installed') from None
    logger = logging.Logger('messaging.notion', level=logging.CRITICAL)
    logger.addHandler(logging.NullHandler())
    return Client(auth=token, notion_version='2025-09-03', timeout_ms=int(timeout * 1000), retry=False, logger=logger)


def _notion_files(task, config):
    files = []
    root = Path(config['storage']['assets_dir']).resolve()
    for resource in task.get('resources', []):
        if not resource.get('local_path'):
            continue
        path = Path(resource['local_path']).resolve()
        if root not in path.parents or not path.is_file():
            raise DeliveryRejected('material_outside_archive_or_missing')
        if path.stat().st_size > 20 * 1024 * 1024:
            raise DeliveryRejected('notion_file_exceeds_single_upload_limit')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != resource.get('sha256'):
                raise DeliveryRejected('material_integrity_mismatch')
        files.append((resource, path))
    if len(files) > 100:
        raise DeliveryRejected('notion_too_many_materials')
    return files


def _notion(target, envelope, config, previous):
    token = _secret(target, 'token_env', config)
    source_id = target.get('data_source_id')
    if not source_id:
        raise DeliveryRejected('notion_data_source_not_configured')
    task = envelope['task']
    names = dict(title='Name', task_id='Task ID', priority='Priority', status='Status',
                 date='Date', instructions='Instructions', files='Materials')
    names.update(target.get('properties', {}))
    if len(set(names.values())) != len(names) or any(not isinstance(n, str) or not n for n in names.values()):
        raise DeliveryRejected('notion_property_mapping_invalid')
    status_type = target.get('status_type', 'select')
    if status_type not in {'select', 'status'}:
        raise DeliveryRejected('notion_status_type_invalid')
    date = task.get('time', {}).get('deadline') or task.get('time', {}).get('start')
    properties = {names['title']: {'title': _rich_text(task['title'])},
        names['task_id']: {'rich_text': _rich_text(task['task_id'])},
        names['priority']: {'select': {'name': task['priority']}},
        names['status']: {status_type: {'name': task['status']}},
        names['date']: {'date': {'start': date} if date else None},
        names['instructions']: {'rich_text': _rich_text(_instructions(task))}}
    instruction_parts = properties[names['instructions']]['rich_text']
    for resource in task.get('resources', []):
        if _url(resource.get('url')):
            instruction_parts.append({'type': 'text', 'text': {'content': '\n' + resource['name'][:1999],
                                                               'link': {'url': resource['url']}}})
    if len(instruction_parts) > 100:
        raise DeliveryRejected('notion_text_too_large')
    files = _notion_files(task, config)
    client = _notion_client(token, target.get('timeout_seconds', 30))
    try:
        # Query all pages before creating: recovered receipts and retried imports reuse the same page.
        matches, cursor, visited = [], None, set()
        while True:
            query = {'data_source_id': source_id, 'filter': {'property': names['task_id'], 'rich_text': {'equals': task['task_id']}}, 'page_size': 100}
            if cursor:
                query['start_cursor'] = cursor
            found = client.data_sources.query(**query)
            matches.extend(found['results'])
            if not found.get('has_more'):
                break
            cursor = found.get('next_cursor')
            if not cursor or cursor in visited:
                raise DeliveryRejected('notion_pagination_invalid')
            visited.add(cursor)
        if len(matches) > 1:
            raise DeliveryRejected('notion_duplicate_task_id')
        if previous and previous.get('remote_id') and (not matches or matches[0]['id'] != previous['remote_id']):
            raise DeliveryRejected('notion_remote_task_changed_or_removed')
        uploads, file_objects = {}, []
        cached = (previous or {}).get('uploads', {})
        for resource, path in files:
            sha = resource['sha256']
            upload_id = cached.get(sha)
            if not upload_id:
                mime = mimetypes.guess_type(resource['name'])[0] or 'application/octet-stream'
                upload = client.file_uploads.create(mode='single_part', filename=resource['name'], content_type=mime)
                with path.open('rb') as stream:
                    sent = client.file_uploads.send(file_upload_id=upload['id'], file=(resource['name'], stream, mime))
                if sent.get('status') != 'uploaded':
                    raise DeliveryRejected('notion_upload_not_complete')
                upload_id = upload['id']
            uploads[sha] = upload_id
            file_objects.append({'name': resource['name'], 'type': 'file_upload', 'file_upload': {'id': upload_id}})
        properties[names['files']] = {'files': file_objects}
        if matches:
            page = client.pages.update(page_id=matches[0]['id'], properties=properties)
        else:
            page = client.pages.create(parent={'data_source_id': source_id}, properties=properties)
        return {'delivery_id': envelope['delivery_id'], 'remote_id': page['id'],
                'url': page.get('url'), 'uploads': uploads}
    except DeliveryError:
        raise
    except Exception as exc:
        status = getattr(exc, 'status', None)
        if isinstance(status, int) and 400 <= status < 500:
            raise DeliveryRejected('notion_request_rejected') from None
        raise DeliveryUncertain('notion_delivery_outcome_unknown') from None
    finally:
        client.close()


def deliver(target, envelope, config, previous=None):
    if not config.get('enabled') or not target.get('enabled'):
        raise DeliveryRejected('delivery_disabled')
    public_envelope(envelope)
    kind = target.get('kind')
    if kind == 'feishu':
        return _feishu(target, envelope, config)
    if kind == 'notion':
        return _notion(target, envelope, config, previous)
    if kind == 'webhook':
        return _webhook(target, envelope, config, previous)
    raise DeliveryRejected('unknown_delivery_channel')
