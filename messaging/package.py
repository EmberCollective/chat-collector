"""Portable task packages and durable material copies; no model or collector imports."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

SOURCE_INLINE_CHARS = 2000


def _source_archive(sources, config, prepare):
    text = '\n\n'.join(' · '.join(str(s.get(k) or '') for k in ('platform', 'conversation', 'sender', 'time'))
                       + '\n' + s.get('text', '') for s in sources)
    data = text.encode('utf-8')
    sha = hashlib.sha256(data).hexdigest()
    root = Path(config['storage']['assets_dir']).resolve()
    path = root / sha[:2] / sha
    item = {'id': sha, 'name': '来源原文.txt', 'type': 'file', 'role': 'input',
            'purpose': '完整来源原文', 'source_refs': [s['ref'] for s in sources],
            'sha256': sha, 'size_bytes': len(data), 'status': 'unavailable'}
    try:
        if prepare and not path.exists():
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=root, prefix='.sources-') as temporary:
                original = Path(temporary) / 'sources.txt'
                original.write_bytes(data)
                _store(original, config, sha)
        if path.exists():
            if not path.resolve().is_relative_to(root) or _hash(path) != sha:
                raise ValueError('material_store_corrupt')
            item.update(status='ready', local_path=str(path))
    except (OSError, ValueError) as exc:
        item['warnings'] = [str(exc) if isinstance(exc, ValueError) else 'material_file_unavailable']
    return item


def _hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _id(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _name(value):
    return str(value or '附件').replace('\\', '/').rsplit('/', 1)[-1]


def safe_url(value):
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme in {'https', 'http'} and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return None


def _store(path, config, expected=None):
    """Copy once per content hash; never link mutable source bytes into task materials."""
    root = Path(config['storage']['assets_dir']).resolve()
    before = path.stat()
    sha = _hash(path)
    if expected and sha != expected:
        raise ValueError('material_hash_mismatch')
    dest = root / sha[:2] / sha
    if dest.exists():
        if not dest.resolve().is_relative_to(root) or _hash(dest) != sha:
            raise ValueError('material_store_corrupt')
        return dest, sha, before.st_size
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.parent.resolve().is_relative_to(root):
        raise ValueError('material_store_outside_root')
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=dest.parent, prefix='.material-', delete=False) as output:
            tmp = Path(output.name)
            with path.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or _hash(tmp) != sha:
            raise ValueError('material_changed_during_copy')
        tmp.replace(dest)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
    return dest, sha, before.st_size


def archive_result(file_path, config):
    """Only used for a file explicitly selected by the local user, never an AI path."""
    path = Path(file_path).resolve(strict=True)
    if not path.is_file() or path.name.startswith('.env') or 'secrets' in {p.lower() for p in path.parts} or path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.key', '.pem'}:
        raise ValueError('result_file_not_allowed')
    dest, sha, size = _store(path, config)
    return {'id': sha, 'name': path.name, 'type': 'file', 'status': 'ready', 'role': 'result',
            'sha256': sha, 'size_bytes': size, 'local_path': str(dest)}


def _material(attachment, source_key, index, config, prepare):
    resource = {'id': _id(f'{source_key}:{index}'), 'name': _name(attachment.get('name')),
                'type': attachment.get('type', 'file'), 'role': 'input',
                'status': 'unavailable', 'source_refs': [source_key], 'warnings': []}
    if attachment.get('quality'):
        resource['quality'] = attachment['quality']
    path_value = attachment.get('archive_path')
    sha = attachment.get('sha256')
    if attachment.get('status') in {'encrypted_only', 'unsupported', 'missing', 'unverified'}:
        resource['warnings'].append('material_' + attachment['status'])
        return resource
    if not path_value or not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha):
        resource['warnings'].append('verified_archive_unavailable')
        reason = attachment.get('reason')
        if isinstance(reason, str) and re.fullmatch('[a-z_]{1,100}', reason):
            resource['warnings'].append(reason)
        return resource
    try:
        store = Path(config['storage']['assets_dir']).resolve()
        stored = store / sha[:2] / sha
        path = (stored if stored.exists() else Path(path_value)).resolve(strict=True)
        roots = [store, *[Path(p).resolve() for p in config.get('attachments', {}).get('allowed_roots', [])]]
        if not path.is_file() or not any(path.is_relative_to(root) for root in roots):
            raise ValueError('material_outside_allowed_roots')
        if _hash(path) != sha:
            raise ValueError('material_hash_mismatch')
        if not Path(resource['name']).suffix:
            suffix = Path(_name(path_value)).suffix.lower()
            if suffix in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.tiff',
                          '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                          '.txt', '.md', '.csv', '.zip', '.7z', '.mp3', '.mp4', '.wav'}:
                if not attachment.get('name'):
                    resource['name'] = ('图片' if resource['type'] == 'image' else '附件') + f'-{index + 1}'
                resource['name'] += suffix
        size = path.stat().st_size
        if prepare:
            path, sha, size = _store(path, config, sha)
        resource.update(id=sha, sha256=sha, size_bytes=size, local_path=str(path), status='ready')
        if attachment.get('status') == 'thumbnail_only':
            resource['warnings'].append('thumbnail_only')
    except (OSError, ValueError) as exc:
        resource['warnings'].append(str(exc) if isinstance(exc, ValueError) else 'material_file_unavailable')
    return resource


def build_package(task, config, *, prepare=False):
    package = {key: deepcopy(task.get(key)) for key in ('task_id', 'revision', 'title', 'event', 'priority',
        'priority_reason', 'status', 'time', 'people', 'location', 'needs_review', 'review_reasons', 'updated_at')}
    package['time'] = {k: (task.get('time') or {}).get(k) for k in ('start', 'deadline', 'timezone', 'text')}
    package['people'] = [{k: p[k] for k in ('name', 'role')} for p in task.get('people', [])]
    package['notes'] = list(dict.fromkeys([*task.get('notes', []), *task.get('review_reasons', [])]))
    package['review_reasons'] = list(package['notes'])
    package['needs_review'] = False
    package.update(schema_version=1, action_steps=deepcopy(task.get('action_steps', [])), resources=[], sources=[])
    selected = {(item['message_key'], item['attachment_index']): item['attachment']
                for item in [*task.get('materials', []), *task.get('attachment_evidence', [])]
                if isinstance(item.get('attachment'), dict)}
    resources = {}
    package['unselected_materials_count'] = 0
    for source in task.get('sources', []):
        key = source['message_key']
        identity = source.get('identity') or {}
        conversation = identity.get('conversation') or {}
        package['sources'].append({'ref': key, 'platform': source.get('collector_module'),
            'conversation': conversation.get('display_name') or conversation.get('remark') or conversation.get('name') or '名称未获取',
            'sender': source.get('sender') or '名称未获取', 'time': source.get('sent_at_local'),
            'text': source.get('text') or ''})
        for index, original in enumerate(source.get('attachments', [])):
            attachment = selected.get((key, index))
            if attachment is None:
                package['unselected_materials_count'] += 1
                continue
            item = _material(dict(attachment, name=attachment.get('name') or original.get('name')),
                             key, index, config, prepare)
            if item['id'] in resources:
                resources[item['id']]['source_refs'] = sorted(set(resources[item['id']]['source_refs'] + [key]))
            else:
                resources[item['id']] = item
        # Links are copied only from cited source messages, not invented model output.
        for match in re.finditer(r'https?://[^\s<>\[\]"\u3000\u3002\uff0c\uff1b\uff01\uff09]+', source.get('text') or ''):
            url = safe_url(match.group().rstrip('.,;!?)'))
            if url:
                identifier = _id(url)
                if identifier not in resources:
                    resources[identifier] = {'id': identifier, 'name': url, 'type': 'link', 'role': 'input',
                                             'status': 'ready', 'url': url, 'source_refs': []}
                resources[identifier]['source_refs'] = sorted(set(resources[identifier]['source_refs'] + [key]))
    package['resources'] = list(resources.values())
    if sum(len(s['text']) for s in package['sources']) > SOURCE_INLINE_CHARS:
        package['resources'].append(_source_archive(package['sources'], config, prepare))
    return package


def material_path(package, resource_id, config):
    """Serve only a registered, hash-verified material; callers cannot supply paths."""
    resource = next((r for r in package.get('resources', []) if r['id'] == resource_id), None)
    if not resource or resource.get('status') != 'ready' or not resource.get('local_path'):
        raise ValueError('material_not_available')
    path = Path(resource['local_path']).resolve(strict=True)
    roots = [Path(config['storage']['assets_dir']).resolve(),
             *[Path(p).resolve() for p in config.get('attachments', {}).get('allowed_roots', [])]]
    if not path.is_file() or not any(path.is_relative_to(root) for root in roots) or _hash(path) != resource.get('sha256'):
        raise ValueError('material_verification_failed')
    return path, resource['name']
