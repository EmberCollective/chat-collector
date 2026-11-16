"""Filtered local run history, independent of the recent in-memory job cache."""
import math
import re
import time
from typing import Literal

from fastapi import Depends, HTTPException, Query

from workflow import find_run_folder, history_roots, read_run_metadata


def _timestamp(value):
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def history_jobs(jobs, processor_history=None):
    roots = history_roots(jobs, processor_history)
    processor_root = roots[1] if len(roots) > 1 else None
    history, seen = [], set()
    now = time.time()
    for root in roots:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if not re.fullmatch(r'[0-9a-f]{32}', entry.name) or entry.name in seen:
                continue
            try:
                folder = find_run_folder(jobs, entry.name, processor_root)
                job = read_run_metadata(folder, jobs)
            except HTTPException as exc:
                if exc.status_code == 409:
                    raise
                continue
            seen.add(entry.name)
            for key in ('module', 'action', 'origin', 'status', 'error'):
                if not isinstance(job.get(key), str):
                    job[key] = None
            start, end = _timestamp(job.get('started_at')), _timestamp(job.get('finished_at'))
            job['started_at'], job['finished_at'] = start, end
            complete = start is not None and end is not None and end >= start and job['status'] != 'running'
            elapsed = end-start if complete else max(0, now-start) if job['status'] == 'running' and start is not None else None
            job.update(duration_seconds=elapsed, duration_complete=complete)
            history.append(job)
    return history


def register_history_routes(app, jobs, authenticate, processor_history=None, bridge=None):
    @app.get('/api/jobs', dependencies=[Depends(authenticate)], summary='筛选、排序并分页查看全部持久化运行记录')
    def history(cursor: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
                action: str = Query('', max_length=100), module: str = Query('', max_length=100),
                origin: str = Query('', max_length=100), status: str = Query('', max_length=100),
                min_duration_seconds: float | None = Query(None, ge=0, allow_inf_nan=False),
                max_duration_seconds: float | None = Query(None, ge=0, allow_inf_nan=False),
                start: float | None = Query(None, ge=0, allow_inf_nan=False),
                end: float | None = Query(None, ge=0, allow_inf_nan=False),
                error: str = Query('', max_length=500),
                sort: Literal['latest', 'duration_asc', 'duration_desc'] = 'latest'):
        if (min_duration_seconds is not None and max_duration_seconds is not None
                and min_duration_seconds > max_duration_seconds):
            raise HTTPException(422, '最短耗时不能大于最长耗时')
        if start is not None and end is not None and start >= end:
            raise HTTPException(422, '结束时间必须晚于开始时间')
        entries = history_jobs(jobs, processor_history)
        warnings = []
        if bridge is not None:
            try:
                from automation.config import load_config, JOBS
                from automation.history import list_runs, MAX_TERMINAL_RUNS
                path = bridge.root / bridge.settings.get('automation_config', 'config/automation.toml')
                if path.is_file():
                    config = load_config(path)
                    for name in JOBS:
                        for record in list_runs(config, module=name, limit=MAX_TERMINAL_RUNS):
                            record.update(source='automation', duration_complete=record['finished_at'] is not None)
                            if record['status'] in {'running', 'retry'}:
                                record['duration_seconds'] = max(0, time.time()-record['started_at'])
                            entries.append(record)
            except Exception as exc:
                warnings.append('后台执行记录暂时无法读取：' + type(exc).__name__)
        episodes = {entry['id'] for entry in entries if entry.get('source') == 'automation'}
        entries = [entry for entry in entries if entry.get('automation_run_id') not in episodes]
        facets = {key+'s': sorted({item[key] for item in entries if item[key]})
                  for key in ('action', 'module', 'origin')}
        filters = {'action': action, 'module': module, 'origin': origin, 'status': status}
        matched = []
        for item in entries:
            if any(value and item[key] != value for key, value in filters.items()):
                continue
            duration, started = item['duration_seconds'], item['started_at']
            if min_duration_seconds is not None and (duration is None or duration < min_duration_seconds):
                continue
            if max_duration_seconds is not None and (duration is None or duration > max_duration_seconds):
                continue
            if start is not None and (started is None or started < start):
                continue
            if end is not None and (started is None or started >= end):
                continue
            if error.casefold() not in (item['error'] or '').casefold():
                continue
            matched.append(item)
        matched.sort(key=lambda item: (item['started_at'] or 0, item['id']), reverse=True)
        if sort != 'latest':
            sign = -1 if sort == 'duration_desc' else 1
            matched.sort(key=lambda item: (item['duration_seconds'] is None, sign*(item['duration_seconds'] or 0)))
        page = matched[cursor:cursor+limit]
        return {'jobs': page, 'total': len(matched), 'facets': facets, 'warnings': warnings,
                'next_cursor': cursor+len(page), 'has_more': cursor+len(page) < len(matched)}
