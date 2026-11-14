"""Authenticated, on-demand access to durable Pi workflow bodies."""
import json
from pathlib import Path
import re

from fastapi import Depends, HTTPException, Query
import psutil


MAX_LINE = 1100000
MAX_PAGE = 4 * 1024 * 1024


def history_roots(jobs, processor_history=None):
    directories = [jobs.directory.resolve()]
    if processor_history is not None:
        try:
            path = processor_history() if callable(processor_history) else processor_history
            directory = Path(path).resolve()
            if directory not in directories:
                directories.append(directory)
        except (OSError, ValueError):
            pass  # Existing console runs remain readable while processor configuration is being repaired.
    return directories


def find_run_folder(jobs, identifier, processor_history=None):
    if not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise HTTPException(404, '运行记录不存在')
    matches = []
    for root in history_roots(jobs, processor_history):
        folder = (root / identifier).resolve()
        if folder.parent == root and folder.is_dir():
            matches.append(folder)
    if len(matches) > 1:
        raise HTTPException(409, '运行编号在两个记录目录中冲突，请检查本地运行记录')
    if not matches:
        raise HTTPException(404, '运行记录不存在')
    return matches[0]


def local_record_file(folder, name):
    path = (folder / name).resolve()
    if path.parent != folder:
        raise HTTPException(404, '运行记录不存在')
    return path


def read_run_metadata(folder, jobs):
    try:
        job = json.loads(local_record_file(folder, 'job.json').read_text(encoding='utf-8'))
        if not isinstance(job, dict):
            raise ValueError('Invalid job metadata')
        result = local_record_file(folder, 'result.json')
        if result.is_file():
            outcome = json.loads(result.read_text(encoding='utf-8'))
            if not isinstance(outcome, dict):
                raise ValueError('Invalid job outcome')
            job.update({key: outcome[key] for key in ('status', 'error') if key in outcome})
        elif job.get('status') == 'running':
            if folder.parent == jobs.directory.resolve():
                active = job.get('id') in getattr(jobs, 'running', {}).values()
            else:
                try:
                    process = psutil.Process(job.get('pid', -1))
                    active = abs(process.create_time() - job.get('create_time', 0)) < .01 and process.is_running() and process.status() != psutil.STATUS_ZOMBIE
                except (psutil.Error, TypeError, ValueError):
                    active = False
            if not active:
                job['status'] = 'interrupted'
        job['id'] = folder.name
        result = {key: job.get(key) for key in ('id', 'module', 'action', 'origin', 'status', 'started_at', 'finished_at', 'error')}
        parent = job.get('automation_run_id')
        if isinstance(parent, str) and re.fullmatch(r'[0-9a-f]{32}', parent):
            result['automation_run_id'] = parent
            if type(job.get('automation_attempt')) is int and job['automation_attempt'] > 0:
                result['automation_attempt'] = job['automation_attempt']
        return result
    except (OSError, ValueError, TypeError):
        raise HTTPException(404, '运行元数据不可读') from None


def register_workflow_routes(app, jobs, authenticate, processor_history=None):

    @app.get('/api/workflow/runs', dependencies=[Depends(authenticate)], summary='查看持久化的 Pi 处理运行')
    def runs(cursor: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
        history = []
        seen = set()
        for root in history_roots(jobs, processor_history):
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if not re.fullmatch(r'[0-9a-f]{32}', entry.name):
                    continue
                try:
                    folder = find_run_folder(jobs, entry.name, processor_history)
                    if folder.name in seen:
                        continue
                    seen.add(folder.name)
                    job = read_run_metadata(folder, jobs)
                    if job['module'] != 'processor' or job['action'] != 'process':
                        continue
                    job['recorded'] = local_record_file(folder, 'pi-workflow.jsonl').is_file()
                    history.append(job)
                except HTTPException as exc:
                    if exc.status_code == 409:
                        raise
                    continue
        history.sort(key=lambda item: (item.get('started_at') or 0, item['id']), reverse=True)
        page = history[cursor:cursor + limit]
        return {'runs': page, 'next_cursor': cursor + len(page), 'has_more': cursor + len(page) < len(history)}

    @app.get('/api/jobs/{identifier}/workflow', dependencies=[Depends(authenticate)], summary='分页读取 Pi 上下文与工具记录')
    def workflow(identifier: str, cursor: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        folder = find_run_folder(jobs, identifier, processor_history)
        job = read_run_metadata(folder, jobs)
        path = local_record_file(folder, 'pi-workflow.jsonl')
        response = {'events': [], 'next_cursor': cursor, 'has_more': False, 'recorded': path.is_file(),
                    'job': job, 'coverage': 'safe_projection',
                    'omissions': ['credentials', 'image_bodies', 'private_reasoning', 'oversized_bodies']}
        if not path.is_file():
            return response
        with path.open('rb') as stream:
            size = path.stat().st_size
            if cursor > size:
                raise HTTPException(422, '读取位置超过记录长度，请重新打开此运行')
            if cursor:
                stream.seek(cursor - 1)
                if stream.read(1) != b'\n':
                    raise HTTPException(422, '读取位置必须来自上一页的 next_cursor')
            stream.seek(cursor)
            page_bytes = 0
            incomplete = False
            while len(response['events']) < limit:
                position = stream.tell()
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    break
                oversized = len(line) > MAX_LINE
                if oversized:
                    while line and not line.endswith(b'\n'):
                        line = stream.readline(MAX_LINE + 1)
                if not line.endswith(b'\n'):
                    incomplete = True
                    break  # An active worker has not finished this record; retain its start cursor.
                length = stream.tell() - position
                if page_bytes + length > MAX_PAGE and response['events']:
                    break
                try:
                    if oversized:
                        raise ValueError('oversized')
                    event = json.loads(line)
                    if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                        raise ValueError('invalid')
                except (ValueError, UnicodeError):
                    event = {'type': 'record_unavailable', 'body': {'omitted': 'oversized_or_invalid_record'}}
                event['offset'] = position
                response['events'].append(event)
                response['next_cursor'] = stream.tell()
                page_bytes += length
            response['has_more'] = response['next_cursor'] < size and not incomplete
            response['pending'] = incomplete
        return response
