"""Read-only view of manual console jobs and durable automation executions."""
import time
from typing import Literal

from fastapi import Depends, HTTPException, Query


Module = Literal['wechat', 'qq', 'processor', 'messaging', 'feedback']
Origin = Literal['manual', 'schedule']
Status = Literal['running', 'retry', 'succeeded', 'warning', 'failed', 'interrupted']


def _manual(job):
    record = dict(job, source='console', attempt_count=1)
    end = record.get('finished_at') or (time.time() if record['status'] == 'running' else None)
    record['duration_seconds'] = max(0, end - record['started_at']) if end is not None else None
    return record


def register_activity_routes(app, bridge, jobs, authenticate):
    def config():
        from automation import load_config
        path = bridge.root / bridge.settings.get('automation_config', 'config/automation.toml')
        return load_config(path) if path.is_file() else None

    @app.get('/api/activity', dependencies=[Depends(authenticate)], summary='统一查询手动与自动执行记录')
    def activity(module: Module | None = None, origin: Origin | None = None,
                 status: Status | None = None, limit: int = Query(100, ge=1, le=100)):
        with jobs.mutex:
            records = [_manual(job) for job in jobs.jobs.values()
                       if all(value is None or job.get(key) == value
                              for key, value in (('module', module), ('origin', origin), ('status', status)))]
        warnings = []
        try:
            from automation.history import list_runs
            automatic = config()
            if automatic:
                records.extend(dict(run, source='automation') for run in list_runs(
                    automatic, limit=limit, module=module, origin=origin, status=status))
        except Exception as exc:
            # One unavailable source must not hide the other source's records.
            warnings.append('后台执行记录暂时无法读取：' + type(exc).__name__)
        records.sort(key=lambda record: (record['started_at'], record['source'], record['id']), reverse=True)
        return {'records': records[:limit], 'warnings': warnings}

    @app.get('/api/activity/{source}/{identifier}', dependencies=[Depends(authenticate)],
             summary='查看同一轮执行及各次重试的结果')
    def detail(source: Literal['console', 'automation'], identifier: str):
        if source == 'console':
            record = _manual(jobs.detail(identifier))
            record['attempts'] = [dict(number=1, **{key: record.get(key) for key in
                ('status', 'started_at', 'finished_at', 'error', 'result')})]
            return record
        try:
            from automation.history import get_run
            automatic = config()
            record = get_run(automatic, identifier) if automatic else None
        except Exception:
            raise HTTPException(503, '后台执行记录暂时无法读取，请检查自动化配置及状态数据库。') from None
        if record is None:
            raise HTTPException(404, '记录不存在或已超过保留范围')
        return dict(record, source='automation')
