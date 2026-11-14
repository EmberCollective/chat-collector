"""Authenticated, on-demand access to durable Pi workflow bodies."""
import json
import re

from fastapi import Depends, HTTPException, Query


MAX_LINE = 1100000
MAX_PAGE = 4 * 1024 * 1024


def register_workflow_routes(app, jobs, authenticate):
    def folder_for(identifier):
        if not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise HTTPException(404, '运行记录不存在')
        root = jobs.directory.resolve()
        folder = (root / identifier).resolve()
        if folder.parent != root or not folder.is_dir():
            raise HTTPException(404, '运行记录不存在')
        return folder

    def local_file(folder, name):
        path = (folder / name).resolve()
        if path.parent != folder:
            raise HTTPException(404, '运行记录不存在')
        return path

    def job_info(folder):
        try:
            job = json.loads(local_file(folder, 'job.json').read_text(encoding='utf-8'))
            if not isinstance(job, dict):
                raise ValueError('Invalid job metadata')
            result = local_file(folder, 'result.json')
            if result.is_file():
                outcome = json.loads(result.read_text(encoding='utf-8'))
                if not isinstance(outcome, dict):
                    raise ValueError('Invalid job outcome')
                job.update({key: outcome[key] for key in ('status', 'error') if key in outcome})
            elif job.get('status') == 'running' and job.get('id') not in getattr(jobs, 'running', {}).values():
                job['status'] = 'interrupted'
            job['id'] = folder.name
            return {key: job.get(key) for key in ('id', 'module', 'action', 'origin', 'status', 'started_at', 'finished_at', 'error')}
        except (OSError, ValueError, TypeError):
            raise HTTPException(404, '运行元数据不可读') from None

    @app.get('/api/workflow/runs', dependencies=[Depends(authenticate)], summary='查看持久化的 Pi 处理运行')
    def runs(cursor: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
        history = []
        for entry in jobs.directory.iterdir():
            if not re.fullmatch(r'[0-9a-f]{32}', entry.name):
                continue
            try:
                folder = folder_for(entry.name)
                job = job_info(folder)
                if job['module'] != 'processor' or job['action'] != 'process':
                    continue
                job['recorded'] = local_file(folder, 'pi-workflow.jsonl').is_file()
                history.append(job)
            except HTTPException:
                continue
        history.sort(key=lambda item: (item.get('started_at') or 0, item['id']), reverse=True)
        page = history[cursor:cursor + limit]
        return {'runs': page, 'next_cursor': cursor + len(page), 'has_more': cursor + len(page) < len(history)}

    @app.get('/api/jobs/{identifier}/workflow', dependencies=[Depends(authenticate)], summary='分页读取 Pi 上下文与工具记录')
    def workflow(identifier: str, cursor: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        folder = folder_for(identifier)
        job = job_info(folder)
        path = local_file(folder, 'pi-workflow.jsonl')
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
                    if not isinstance(event, dict):
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
