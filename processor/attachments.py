"""Bounded local attachment context; model tools accept registered refs, never paths."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

import psutil


DEFAULTS = {'max_file_bytes': 32 * 1024 * 1024, 'max_pages': 200,
            'max_pixels': 20_000_000, 'max_text_chars': 1_000_000,
            'max_blocks': 2000, 'timeout_seconds': 45, 'max_memory_mb': 1024}
CACHE_VERSION = 1


def _safe_child(root, *parts):
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root):
        raise ValueError('cache_path_outside_root')
    return path


def _stop_worker(process):
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in reversed(children):
        try:
            child.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _run_worker(directory, request, limits):
    """Isolate native parsers/converters with a deadline and measured process-tree RSS."""
    request = dict(request, limits=limits)
    with tempfile.TemporaryDirectory(prefix='parse-', dir=directory) as temp:
        work = Path(temp)
        request['result'] = str(work / 'result.json')
        request_path = work / 'request.json'
        request_path.write_text(json.dumps(request), encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).with_name('attachment_worker.py')), str(request_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + limits['timeout_seconds']
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise ValueError('attachment_parser_timeout')
                try:
                    root = psutil.Process(process.pid)
                    memory = sum(p.memory_info().rss for p in [root, *root.children(recursive=True)] if p.is_running())
                    if memory > limits['max_memory_mb'] * 1024 * 1024:
                        raise ValueError('attachment_parser_memory_limit')
                except psutil.NoSuchProcess:
                    pass
                time.sleep(.05)
            result = work / 'result.json'
            if process.returncode or not result.is_file() or result.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('attachment_parser_failed')
            response = json.loads(result.read_text(encoding='utf-8'))
            if response.get('error'):
                raise ValueError(response['error'])
            return response
        finally:
            if process.poll() is None:
                _stop_worker(process)


def _pages(value, count):
    if value is None:
        return list(range(1, min(count, 3) + 1))
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*', value) or len(value) > 100:
        raise ValueError('pages_must_be_numbers_or_ranges_like_1-3,5')
    chosen = set()
    for part in value.split(','):
        bounds = list(map(int, part.split('-')))
        first, last = bounds[0], bounds[-1]
        if first < 1 or last < first or last > count or last - first >= 10:
            raise ValueError('page_range_out_of_bounds_or_over_10_units')
        chosen.update(range(first, last + 1))
    if len(chosen) > 10:
        raise ValueError('read_at_most_10_units_per_call')
    return sorted(chosen)


class AttachmentContext:
    """One model invocation's registered archive files and actually-read evidence."""

    def __init__(self, events, config):
        settings = config.get('attachments', {})
        self.limits = {name: settings.get(name, value) for name, value in DEFAULTS.items()}
        if any(type(value) is not int or value <= 0 for value in self.limits.values()):
            raise ValueError('attachment_limits_must_be_positive_integers')
        self.roots = [Path(p).resolve() for p in settings.get('archive_roots', [])]
        self.cache = Path(settings.get('cache_dir') or Path(config['storage']['state_file']).parent / 'attachment-cache').resolve()
        self.vision = config.get('model', {}).get('vision', False) is True
        self.enabled = settings.get('enabled', True) is True
        self.config = config
        from .attachment_resolver import AttachmentResolver
        self.resolver = AttachmentResolver(config)
        self.resolved = {}
        self.files, self.evidence = {}, {}
        by_content = {}
        for event in events:
            for attachment_index, attachment in enumerate(event.get('message', {}).get('attachments', [])):
                if not isinstance(attachment, dict):
                    continue
                ref = 'f' + str(len(self.files) + 1)
                name = str(attachment.get('name') or 'attachment').replace('\\', '/').rsplit('/', 1)[-1][:200]
                entry = {'ref': ref, 'name': name, 'type': attachment.get('type', 'file'),
                         'status': 'available', 'content_status': 'unread', 'message_keys': [event['message_key']],
                         'origins': [{'message_key': event['message_key'], 'attachment_index': attachment_index}],
                         'needs_review': False, 'warnings': [], 'vision_enabled': self.vision,
                         'total_units': None, 'read_units': [], 'complete_document': False}
                self.files[ref] = {'catalog': entry, 'event': event, 'attachment_index': attachment_index}
                try:
                    if not self.enabled:
                        raise ValueError('attachment_context_disabled')
                    source = attachment.get('archive_path')
                    if not isinstance(source, str) or not source:
                        raise ValueError('attachment_not_archived')
                    path = Path(source).resolve(strict=True)
                    if not any(path.is_relative_to(root) for root in self.roots):
                        raise ValueError('attachment_outside_archive_roots')
                    stat = path.stat()
                    if not path.is_file() or stat.st_size > self.limits['max_file_bytes']:
                        raise ValueError('attachment_file_size_limit')
                    with path.open('rb') as stream:
                        opened = os.fstat(stream.fileno())
                        if (opened.st_ino, opened.st_size) != (stat.st_ino, stat.st_size):
                            raise ValueError('attachment_changed_during_read')
                        raw = stream.read(self.limits['max_file_bytes'] + 1)
                    if len(raw) != stat.st_size or path.stat().st_mtime_ns != stat.st_mtime_ns:
                        raise ValueError('attachment_changed_during_read')
                    sha = hashlib.sha256(raw).hexdigest()
                    if attachment.get('sha256') and attachment['sha256'].lower() != sha:
                        raise ValueError('attachment_archive_hash_mismatch')
                    if sha in by_content:
                        previous = self.files[by_content[sha]]['catalog']
                        if event['message_key'] not in previous['message_keys']:
                            previous['message_keys'].append(event['message_key'])
                        origin = {'message_key': event['message_key'], 'attachment_index': attachment_index}
                        if origin not in previous['origins']:
                            previous['origins'].append(origin)
                        del self.files[ref]
                        continue
                    from .attachment_worker import detect_kind
                    kind = detect_kind(raw, name)
                    version = hashlib.sha256(json.dumps([CACHE_VERSION, self.limits], sort_keys=True).encode()).hexdigest()[:12]
                    self.cache.mkdir(parents=True, exist_ok=True)
                    folder = _safe_child(self.cache, sha, version)
                    folder.mkdir(parents=True, exist_ok=True)
                    target = _safe_child(self.cache, sha, version, 'input.' + kind)
                    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
                            temporary = Path(stream.name)
                            stream.write(raw)
                        temporary.replace(target)
                    entry.update(type=kind, sha256=sha, byte_size=len(raw))
                    self.files[ref].update(path=target, folder=folder)
                    by_content[sha] = ref
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    reason = str(exc) if isinstance(exc, ValueError) else 'attachment_unavailable'
                    entry.update(status='needs_review', content_status='unavailable', needs_review=True, warnings=[reason])
                    if self.enabled and reason in {'attachment_not_archived', 'attachment_unavailable'} and event['message'].get('platform') in self.resolver.paths:
                        entry.update(status='resolvable', content_status='unread', needs_review=False, warnings=[])

    def catalog(self):
        return [dict(item['catalog']) for item in self.files.values()]

    def _prepare(self, item):
        if 'document' in item:
            return item['document']
        folder = item['folder']
        manifest = _safe_child(self.cache, folder.relative_to(self.cache), 'document.json')
        if manifest.exists() and manifest.stat().st_size <= 16 * 1024 * 1024:
            document = json.loads(manifest.read_text(encoding='utf-8'))
        else:
            document = _run_worker(folder, {'operation': 'inspect', 'path': str(item['path']),
                'kind': item['catalog']['type'], 'cache': str(folder)}, self.limits)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, ensure_ascii=False)
            temporary.replace(manifest)
        item['document'] = document
        item['catalog'].update(unit=document['unit'], content_status='parsed', total_units=len(document['units']),
            **{('page_count' if document['unit'] == 'page' else 'block_count'): len(document['units'])})
        item['catalog']['warnings'] = document.get('warnings', [])
        item['catalog']['needs_review'] = bool(document.get('warnings'))
        return document

    def read_attachment(self, ref, pages=None, query=None):
        if not isinstance(ref, str) or ref not in self.files:
            return self._failure('unknown_attachment_ref')
        item = self.files[ref]
        entry = item['catalog']
        item['requested'] = True
        if entry['status'] == 'resolvable':
            try:
                attachment = self.resolver.resolve(item['event'], item['attachment_index'])
                if not attachment.get('archive_path'):
                    # Collector details remain local; never echo a path/error string to the model.
                    item['resolution'] = attachment
                    reason = attachment.get('reason')
                    raise ValueError(reason if reason in {'local_source_not_found','ambiguous_local_candidates',
                        'image_decode_unavailable','image_source_not_found','attachment_path_outside_allowed_roots'} else 'local_attachment_unavailable')
                registered = AttachmentContext([{'message_key':item['event']['message_key'],
                    'message':{'attachments':[attachment]}}], self.config)
                fresh = next(iter(registered.files.values()))
                if fresh['catalog']['status'] != 'available':
                    raise ValueError(fresh['catalog']['warnings'][0])
                original = {k:entry[k] for k in ('ref', 'message_keys', 'origins')}
                entry.update(fresh['catalog'], **original)
                item.update({k:v for k,v in fresh.items() if k in {'path','folder'}})
                for origin in entry['origins']:
                    self.resolved[(origin['message_key'], origin['attachment_index'])] = attachment
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                reason = str(exc) if isinstance(exc, ValueError) and str(exc).replace('_','').isalnum() else 'attachment_resolution_failed'
                entry.update(status='needs_review', content_status='unavailable', needs_review=True, warnings=[reason])
        if entry['status'] != 'available':
            return self._failure(entry['warnings'][0], ref)
        try:
            if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 200):
                raise ValueError('query_must_be_1_to_200_characters')
            document = self._prepare(item)
            units = document['units']
            selected = _pages(pages, len(units)) if pages is not None or not query else [u['index'] for u in units if query.casefold() in u['text'].casefold()][:10]
            warnings = list(document.get('warnings', []))
            if query and not selected and any(u.get('visual') for u in units):
                warnings.append('text_search_does_not_cover_unrecognized_images')
            visual = [index for index in selected if units[index-1].get('visual')]
            images = {}
            if visual and self.vision:
                rendered = _run_worker(item['folder'], {'operation': 'render', 'path': str(item['path']),
                    'kind': entry['type'], 'cache': str(item['folder']), 'indices': visual[:3],
                    'document': document}, self.limits)
                images = {int(k): v for k, v in rendered['images'].items()}
                if len(visual) > 3:
                    warnings.append('image_limit_read_remaining_pages_separately')
            elif visual:
                warnings.append('visual_content_requires_vision_model_or_local_ocr')
            content, evidence = [], []
            fully_read = set(entry['read_units'])
            budget = 20000
            for index in selected:
                unit = units[index-1]
                evidence_ref = f"{ref}:{'p' if document['unit'] == 'page' else 'b'}{index}"
                text = unit['text']
                if query and text:
                    position = text.casefold().find(query.casefold())
                    if position >= 0:
                        text = text[max(0, position-1000):position+3000]
                truncated = len(text) > budget or (query is not None and len(text) != len(unit['text']))
                text = text[:budget]
                budget -= len(text)
                if truncated:
                    warnings.append('text_excerpt_not_full_unit')
                content.append({'type': 'text', 'text': f'[{evidence_ref}]\n' + (text or ('[视觉内容尚未识别]' if unit.get('visual') else '[本页/块没有可提取文本]'))})
                image = images.get(index)
                if image:
                    path = _safe_child(self.cache, item['folder'].relative_to(self.cache), image)
                    if path.stat().st_size > 12 * 1024 * 1024:
                        raise ValueError('rendered_image_size_limit')
                    content.append({'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(path.read_bytes()).decode()})
                proof = {'ref': evidence_ref, 'attachment_ref': ref, 'sha256': entry['sha256'],
                    'message_keys': entry['message_keys'], document['unit']: index,
                    'coverage': 'excerpt' if truncated else 'text_and_image' if image and text else 'image' if image else 'text' if text else 'unread_visual' if unit.get('visual') else 'empty'}
                # An unavailable image is not evidence that the document is empty.
                if text or image:
                    evidence.append(proof)
                    self.evidence[evidence_ref] = dict(proof, origins=entry['origins'])
                if not truncated and (not unit.get('visual') or image):
                    fully_read.add(index)
            entry.update(read_units=sorted(fully_read),
                complete_document=len(fully_read) == len(units) and not document.get('warnings'),
                needs_review=bool(warnings), warnings=sorted(set(warnings)), content_status='read')
            details = {'status': 'needs_review' if warnings else 'ok', 'attachment_ref': ref,
                'unit': document['unit'], 'total_units': len(units), 'selected_units': selected,
                'read_units': entry['read_units'],
                'remaining_units': [u['index'] for u in units if u['index'] not in fully_read],
                'complete_document': entry['complete_document'],
                'warnings': sorted(set(warnings)), 'evidence': evidence}
            details['next_pages'] = ','.join(str(n) for n in details['remaining_units'][:3]) or None
            content.insert(0, {'type': 'text', 'text': json.dumps(details, ensure_ascii=False)})
            return {'content': content, 'details': details}
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else 'attachment_parse_failed'
            entry.update(needs_review=True, warnings=[reason], content_status='error')
            return self._failure(reason, ref)

    @staticmethod
    def _failure(reason, ref=None):
        details = {'status': 'needs_review', 'attachment_ref': ref, 'warnings': [reason], 'evidence': [], 'complete_document': False,
                   'next_action': 'Use available message evidence; report this attachment as unavailable if needed for the task. Do not retry the same unavailable file or invent its contents.'}
        return {'content': [{'type': 'text', 'text': json.dumps(details, ensure_ascii=False)}], 'details': details}
