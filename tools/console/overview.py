"""Read-only overview of retained executions, current queues and project files."""
from contextlib import closing
from collections import OrderedDict
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import threading
import tomllib

from fastapi import Depends, Query

from storage_metrics import StorageMetrics, _stat as _local_stat


MAX_CONSOLE_RECORDS = 2000
MAX_JSON_BYTES = 256 * 1024
SKIPS = {'no_enabled_rules', 'no_new_time_window', 'waiting_for_refresh'}


def _number(value):
    return value if type(value) in (int, float) and 0 <= value < 10 ** 15 and math.isfinite(value) else None


def _count(value):
    return value if type(value) is int and 0 <= value < 2 ** 63 else None


def _json(path):
    if _local_stat(path).st_size > MAX_JSON_BYTES:
        raise ValueError('record_unavailable')
    with path.open('rb') as stream:
        value = json.loads(stream.read(MAX_JSON_BYTES + 1))
    if not isinstance(value, dict):
        raise ValueError('invalid_record')
    return value


def _result(value):
    if isinstance(value, str):
        if len(value) > MAX_JSON_BYTES:
            return None
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _reference(record):
    return {'run_id': record['id'], 'run_source': record['source'], 'source_id': record.get('source_id'),
            'detail_target': {'kind': 'run', 'id': record['id'], 'source': record['source']}}


def _outcome(record):
    status, result = record.get('status'), record.get('result')
    if status in {'running', 'retry'}:
        return status
    start_at, end_at = _number(record.get('started_at')), _number(record.get('finished_at'))
    if start_at is None or end_at is None or end_at < start_at:
        return 'unknown'
    if status == 'failed':
        return 'failed'
    if status != 'succeeded' or result is None:
        return 'unknown'
    if isinstance(result.get('status'), str) and result['status'] in SKIPS:
        return 'skipped'
    start, end = _number(result.get('start')), _number(result.get('end'))
    if start is not None and end is not None and start < end and all(_count(result.get(k)) is not None for k in ('emitted', 'duplicates')):
        return 'success'
    # Automation kept numeric stats only: skipped rounds have no selection window.
    if record['source'] == 'automation' and start is None and end is None and set(result) <= {'matched', 'emitted', 'duplicates', 'batches', 'retry_after_seconds'} and result:
        return 'skipped'
    return 'unknown'


def _metrics(records, *, available=True, complete=True):
    values = {key: 0 for key in ('success', 'failed', 'skipped', 'unknown', 'running', 'retrying')}
    durations, recent, emitted, duplicates = [], [], [], []
    complete = available and complete
    attempts = {'total': 0, 'failed': 0, 'unknown': 0}
    for record in records:
        outcome = _outcome(record)
        values['retrying' if outcome == 'retry' else outcome] += 1
        attempt = record.get('attempts', {})
        for key in attempts:
            attempts[key] += attempt.get(key, 0)
        stats = record.get('result') or {}
        if outcome in {'running', 'retry'}:
            complete = False
        else:
            for name, target in (('emitted', emitted), ('duplicates', duplicates)):
                value = _count(stats.get(name))
                if value is not None:
                    target.append(value)
                elif outcome != 'skipped':
                    complete = False
        if outcome in {'success', 'failed'}:
            start, end = _number(record.get('started_at')), _number(record.get('finished_at'))
            duration = end - start if start is not None and end is not None and end >= start else None
            if duration is not None:
                durations.append(duration)
            recent.append(dict(_reference(record), outcome=outcome, started_at=start, finished_at=end,
                               duration_seconds=duration))
    valid = values['success'] + values['failed']
    durations.sort()
    recent.sort(key=lambda item: item['started_at'] or 0, reverse=True)
    return dict(values, metrics_available=available, emitted=sum(emitted) if emitted or complete else None,
                duplicates=sum(duplicates) if duplicates or complete else None, emitted_complete=complete,
                valid_count=valid, success_rate=values['success'] / valid if valid else None,
                avg_seconds=sum(durations) / len(durations) if durations else None,
                p95_seconds=durations[math.ceil(.95 * len(durations)) - 1] if durations else None,
                sample_count=len(durations), recent=list(reversed(recent[:20])),
                attempts=dict(attempts, unit='attempt', scope='recorded_status_only'))


def _console_records(jobs, read_record=_json):
    with jobs.mutex:
        records = {identifier: dict(record) for identifier, record in jobs.jobs.items()}
    examined, truncated, unreadable = 0, False, 0
    deadline = time.monotonic() + .5
    try:
        with os.scandir(jobs.directory) as stream:
            for entry in stream:
                examined += 1
                if examined > MAX_CONSOLE_RECORDS or time.monotonic() > deadline:
                    truncated = True
                    break
                if not re.fullmatch(r'[0-9a-f]{32}', entry.name) or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    metadata = read_record(Path(entry.path) / 'job.json')
                    metadata['id'] = entry.name
                    records.setdefault(entry.name, metadata)
                except (OSError, ValueError, TypeError):
                    unreadable += 1
    except OSError:
        unreadable += 1
    result = []
    deadline = time.monotonic() + .5
    for record in sorted(records.values(), key=lambda r: _number(r.get('started_at')) or 0, reverse=True):
        identifier = record.get('id')
        if not isinstance(identifier, str):
            continue
        record.update(source='console', result=None, attempts={'total': 1, 'failed': int(record.get('status') == 'failed'), 'unknown': 0})
        if record.get('action') == 'collect' and re.fullmatch(r'[0-9a-f]{32}', identifier):
            try:
                if time.monotonic() > deadline:
                    truncated = True
                    raise ValueError('result_read_budget')
                outcome = read_record(jobs.directory / identifier / 'result.json')
                record['result'] = _result(outcome.get('result'))
                record['status'] = outcome.get('status', record.get('status'))
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError):
                unreadable += 1
        if record.get('status') == 'running' and identifier not in jobs.running.values():
            record['status'] = 'interrupted'
        for field in ('module', 'action', 'origin', 'status', 'error', 'automation_run_id'):
            if not isinstance(record.get(field), str):
                record[field] = None
        for field in ('started_at', 'finished_at'):
            record[field] = _number(record.get(field))
        result.append(record)
    return result, {'available': unreadable == 0, 'examined': examined, 'records': len(result),
                    'scan_limit': MAX_CONSOLE_RECORDS, 'truncated': truncated, 'unreadable': unreadable}


def _automatic(config, start):
    from automation.core import _connect
    from automation.history import MAX_TERMINAL_RUNS
    result, coverage = [], []
    conn = _connect(config)
    if conn is None:
        return [], {'available': False, 'status': 'state_missing', 'modules': [], 'truncated': False}
    with closing(conn):
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'runs', 'run_attempts'} <= tables:
            return [], {'available': False, 'status': 'history_not_provided', 'modules': [], 'truncated': False}
        for module in config['jobs']:
            rows = conn.execute('SELECT * FROM runs WHERE module=? ORDER BY started_at DESC,rowid DESC LIMIT ?',
                                (module, MAX_TERMINAL_RUNS + 1)).fetchall()
            attempts_by_run = {item['run_id']: dict(item) for item in conn.execute('''
                SELECT a.run_id,count(*) AS total,sum(a.status='failed') AS failed,
                       sum(a.status NOT IN ('failed','succeeded','running')) AS unknown
                FROM run_attempts a JOIN
                  (SELECT id FROM runs WHERE module=? ORDER BY started_at DESC,rowid DESC LIMIT ?) r
                ON r.id=a.run_id GROUP BY a.run_id''', (module, MAX_TERMINAL_RUNS + 1))}
            terminal = sum(row['finished_at'] is not None for row in rows)
            earliest = min((_number(row['started_at']) for row in rows if _number(row['started_at']) is not None), default=None)
            earliest_terminal = min((_number(row['started_at']) for row in rows if row['finished_at'] is not None and _number(row['started_at']) is not None), default=None)
            capped = terminal >= MAX_TERMINAL_RUNS
            coverage.append({'module': module, 'retained': len(rows), 'earliest_started_at': earliest,
                             'earliest_terminal_started_at': earliest_terminal,
                             'retention_at_limit': capped, 'window_may_be_truncated': capped and (earliest_terminal is None or earliest_terminal >= start)})
            for row in rows:
                item = dict(row, source='automation')
                for field in ('started_at', 'finished_at'):
                    item[field] = _number(item[field])
                item['result'] = _result(item.get('result'))
                attempts = attempts_by_run.get(item['id'], {})
                item['attempts'] = {key: attempts.get(key, 0) or 0 for key in ('total', 'failed', 'unknown')}
                result.append(item)
    return result, {'available': True, 'status': 'retained_only', 'modules': coverage,
                    'retention_per_module': MAX_TERMINAL_RUNS,
                    'truncated': any(item['window_may_be_truncated'] for item in coverage)}


def _config_path(bridge, module):
    return bridge.root / bridge.settings.get(module + '_config', 'config/' + module + '.toml')


def _specs(path, config, fields, source_id=None):
    # Read original spelling before config loaders resolve symlinks/junctions.
    raw = tomllib.loads(Path(path).read_text(encoding='utf-8'))
    result = []
    for section, name, category in fields:
        declared = raw.get(section, {}).get(name)
        value = declared if declared is not None else config.get(section, {}).get(name)
        if not isinstance(value, str) or not value:
            continue
        location = os.path.abspath(Path(path).parent / (Path(value).expanduser() if source_id else value))
        item = {'path': location, 'category': category, 'source_ids': [source_id] if source_id else [],
                'declared': declared is not None}
        result.append(item)
        if category == 'state':
            result.extend(dict(item, path=location + suffix, optional=True) for suffix in ('-wal', '-shm'))
    return result


def _table_exists(path, table):
    if not Path(path).is_file():
        return False
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=1)) as conn:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _queue_rows(path, table, columns, states):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=1)) as conn:
        conn.row_factory = sqlite3.Row
        marks = ','.join('?' for _ in states)
        return [dict(row) for row in conn.execute(
            f'SELECT {columns} FROM {table} WHERE status IN ({marks}) ORDER BY rowid DESC LIMIT 101', states)]


def _warning(warnings, code, module=None):
    warnings.append({'code': code, 'module': module})


def _issue(kind, module, *, source_id=None, run=None, identifier=None, error=None, last_at=None, status=None):
    if run:
        target = _reference(run)['detail_target']
    elif identifier:
        target = {'kind': kind, 'id': identifier}
    else:
        target = {'kind': 'config', 'id': source_id or module}
    return {'key': ':'.join((kind, source_id or module, identifier or error or 'unknown')),
            'kind': kind, 'module': module, 'source_id': source_id,
            'run_id': run['id'] if run else None, 'run_source': run['source'] if run else None,
            'detail_target': target, 'last_at': last_at, 'error': error, 'status': status,
            'next_action': 'verify_delivery' if kind == 'delivery' and status == 'unknown' else 'inspect_record',
            'next_action_label': '先在目标端核对是否收到，再决定如何处理' if kind == 'delivery' and status == 'unknown' else '查看相关记录与配置，确认原因后再处理',
            'config_target': {'kind': 'config', 'id': source_id or module},
            **({kind + '_id': identifier} if identifier else {})}


class Overview:
    def __init__(self, bridge, jobs):
        self.bridge, self.jobs = bridge, jobs
        self.storage = StorageMetrics()
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._records = OrderedDict()
        self._record_bytes = 0
        self._base_paths = {Path(jobs.directory), Path(bridge.settings_path), *map(Path, bridge.paths.values()),
                            *(_config_path(bridge, module) for module in ('automation', 'processor', 'messaging'))}
        self._tracked = set(self._base_paths)

    @staticmethod
    def _version(path):
        try:
            info = path.stat()
            return info.st_mtime_ns, info.st_size, info.st_ino
        except OSError:
            return None

    def _read_record(self, path):
        version = self._version(path)
        cached = self._records.get(path)
        if cached and cached[0] == version:
            self._records.move_to_end(path)
            return deepcopy(cached[1])
        value = _json(path)
        if path.name == 'result.json':
            result = _result(value.get('result'))
            fields = ('start', 'end', 'emitted', 'duplicates', 'matched', 'batches', 'retry_after_seconds', 'status')
            value = {**({'status': value['status']} if 'status' in value else {}),
                     'result': {key: result[key] for key in fields if key in result} if result is not None else None}
        else:
            fields = ('id', 'module', 'action', 'origin', 'status', 'started_at', 'finished_at', 'error', 'automation_run_id')
            value = {key: value[key] for key in fields if key in value}
        size = len(json.dumps(value, ensure_ascii=False).encode('utf8'))
        previous = self._records.pop(path, None)
        if previous:
            self._record_bytes -= previous[2]
        self._records[path] = (version, value, size)
        self._record_bytes += size
        while len(self._records) > 4096 or self._record_bytes > 2 * 1024 * 1024:
            self._record_bytes -= self._records.popitem(last=False)[1][2]
        return deepcopy(value)

    def invalidate(self):
        with self._lock:
            self._cache.clear()

    def _signature(self):
        with self.jobs.mutex:
            active = tuple((identifier, item.get('status'), item.get('finished_at'))
                           for identifier, item in self.jobs.jobs.items())
        paths = self._tracked | self._records.keys() | {
            *map(Path, self.bridge.paths.values()),
            *(_config_path(self.bridge, module) for module in ('automation', 'processor', 'messaging'))}
        return (repr(self.bridge.settings), repr(self.bridge.sources), active,
                tuple((str(path), self._version(path)) for path in sorted(paths)))

    def snapshot(self, hours):
        with self._lock:
            signature = self._signature()
            cached = self._cache.get(hours)
            if cached and time.monotonic() - cached[0] < 5 and cached[1] == signature:
                return deepcopy(cached[2])
            result = self._snapshot(hours)
            # A writer may finish during the scan; only reuse the input version we started with.
            self._cache[hours] = (time.monotonic(), signature, result)
            self._cache.move_to_end(hours)
            while len(self._cache) > 4:
                self._cache.popitem(last=False)
            return deepcopy(result)

    def _snapshot(self, hours):
        now = time.time()
        start = now - hours * 3600
        bridge, warnings, specs, issues = self.bridge, [], [], []
        sources = list(bridge.sources.values())
        configs = {}
        for source in sources:
            identifier = source['id']
            if identifier not in bridge.paths:
                continue
            try:
                cfg = bridge.config(identifier)
                configs[identifier] = cfg
                specs.extend(_specs(bridge.paths[identifier], cfg, (
                    ('storage', 'state_file', 'state'), ('source', 'decrypted_dir', 'snapshots'),
                    ('storage', 'output_dir', 'output'), ('attachments', 'archive_dir', 'attachments')), identifier))
            except Exception as exc:
                _warning(warnings, type(exc).__name__, identifier)
        manual, manual_coverage = _console_records(self.jobs, self._read_record)
        automatic, auto_cfg = [], None
        auto_coverage = {'available': False, 'status': 'not_configured', 'modules': [], 'truncated': False}
        service = {'state': 'unknown', 'running': None, 'verified': False, 'enabled': None,
                   'config_changed': None, 'pid': None, 'note': '后台服务状态未提供'}
        live_jobs, job_sources = [], {}
        try:
            from automation import load_config, control
            path = _config_path(bridge, 'automation')
            if path.is_file():
                auto_cfg = load_config(path)
                automatic, auto_coverage = _automatic(auto_cfg, start)
                for module, entry in auto_cfg['jobs'].items():
                    matches = [identifier for identifier, config_path in bridge.paths.items() if Path(entry['config']) == config_path]
                    job_sources[module] = matches[0] if len(matches) == 1 else None
                live = control.status(auto_cfg)
                service.update({key: live.get(key) for key in service if key != 'note'})
                service['note'] = '进程状态与工作结果分别统计；运行中不代表各任务成功'
                specs.extend(_specs(path, auto_cfg, (('storage', 'state_file', 'state'),)))
                for module, entry in auto_cfg['jobs'].items():
                    source_id = job_sources[module]
                    current = next((j for j in live.get('jobs', []) if j['name'] == module), {})
                    item = {'module': module, 'source_id': source_id, 'enabled': entry['enabled'],
                            'state': current.get('state', 'unknown'),
                            **{key: current.get(key) for key in ('last_start', 'last_success', 'next_run', 'current_run_id', 'error')},
                            'config_target': {'kind': 'config', 'id': source_id or module}}
                    live_jobs.append(item)
                    for key in ('last_start', 'last_success', 'next_run'):
                        item[key] = _number(item[key])
                    if item['enabled'] and (item['error'] or item['state'] in {'paused', 'retry'}):
                        run = next((run for run in automatic if run['id'] == item['current_run_id']), None)
                        issues.append(_issue('job', module, source_id=source_id, run=run,
                                             error=item['error'] or item['state'], last_at=item['last_start']))
                if live.get('config_changed'):
                    issues.append(_issue('config', 'automation', error='config_changed'))
                if live.get('enabled') and live.get('state') in {'stopped', 'unknown', 'duplicate'}:
                    issues.append(_issue('service', 'automation', error='service_' + live['state']))
        except Exception as exc:
            _warning(warnings, type(exc).__name__, 'automation')
        for run in automatic:
            run['source_id'] = job_sources.get(run['module'])
        parents = {run['id'] for run in automatic}
        records = automatic + [dict(run, source_id=run.get('module') if run.get('module') in bridge.sources else None)
                               for run in manual if run.get('automation_run_id') not in parents]
        formal = [run for run in records if run.get('action') == 'collect'
                  and _number(run.get('started_at')) is not None and start <= run['started_at'] < now]
        unmapped = sum(run.get('source_id') not in configs for run in formal)
        if unmapped:
            _warning(warnings, 'collection_source_unmapped')
        history_complete = manual_coverage['available'] and not manual_coverage['truncated'] and (
            auto_cfg is None or auto_coverage['available'] and not auto_coverage['truncated'])
        measured = []
        for source in sources:
            identifier = source['id']
            runs = [run for run in formal if run.get('source_id') == identifier]
            available = identifier in configs and (manual_coverage['available'] or auto_coverage['available'])
            stats = _metrics(runs, available=available, complete=history_complete)
            current = next((j for j in live_jobs if j['source_id'] == identifier), None)
            state = 'unsupported' if identifier not in bridge.paths else 'unknown'
            if current:
                state = current['state']
                if state == 'idle':
                    state = 'waiting' if current['enabled'] else 'disabled'
                elif not current['enabled'] and state != 'running':
                    state = 'disabled'
            with self.jobs.mutex:
                if identifier in self.jobs.running:
                    state = 'running'
            last = max((r['finished_at'] for r in records if r.get('source_id') == identifier and r.get('action') == 'collect'
                        and _outcome(r) == 'success'), default=None)
            measured.append(dict(id=identifier, name=source['name'], kind=source['kind'],
                                 status=state, last_success=last, **stats))
            candidates = [r for r in records if r.get('source_id') == identifier and r.get('action') == 'collect'
                          and _outcome(r) in {'failed', 'unknown'}
                          and (_number(r.get('finished_at')) or _number(r.get('started_at')) or 0) > (last or 0)
                          and not (r['source'] == 'automation' and r.get('origin') != 'manual' and current and not current['enabled'])]
            if candidates and not any(issue['source_id'] == identifier for issue in issues):
                run = max(candidates, key=lambda r: _number(r.get('finished_at')) or _number(r.get('started_at')) or 0)
                issues.append(_issue('job', identifier, source_id=identifier, run=run,
                                     error=run.get('error') or 'result_unknown',
                                     last_at=run.get('finished_at') or run.get('started_at'), status=_outcome(run)))
        for job in live_jobs:
            job['last_callback_success'] = job['last_success']
            successes = [run for run in automatic if run['module'] == job['module']
                         and (_outcome(run) == 'success' if run['action'] == 'collect' else
                              run['status'] == 'succeeded' and bool(run['result']))]
            job['last_success'] = max((run['finished_at'] for run in successes if _number(run['finished_at']) is not None), default=None)
        with self.jobs.mutex:
            for module, identifier in self.jobs.running.items():
                running = self.jobs.jobs.get(identifier, {})
                job = next((job for job in live_jobs if job['source_id'] == module or job['module'] == module), None)
                if job is None:
                    job = {'module': module, 'source_id': module if module in bridge.sources else None, 'enabled': None,
                           'last_success': None, 'next_run': None, 'error': None,
                           'config_target': {'kind': 'config', 'id': module}}
                    live_jobs.append(job)
                job.update(automation_state=job.get('state'), state='running', manual_run_id=identifier,
                           current_run_id=identifier, last_start=running.get('started_at'), origin='manual')
        summary = _metrics([run for run in formal if run.get('source_id') in configs],
                           available=any(source['metrics_available'] for source in measured),
                           complete=history_complete and not unmapped and all(source['metrics_available'] for source in measured))
        summary.update(sources_measured=sum(s['metrics_available'] for s in measured), sources_total=len(sources))
        bins = []
        for index in range(math.ceil(hours / 2)):
            left, right = start + index * 7200, min(now, start + (index + 1) * 7200)
            selected = [run for run in formal if run.get('source_id') in configs and left <= run['started_at'] < right]
            stats = _metrics(selected)
            failed_refs = [_reference(r) for r in selected if _outcome(r) == 'failed']
            bins.append({**{key: stats[key] for key in ('success', 'failed', 'success_rate', 'sample_count')},
                         'start': left, 'end': right, 'run_refs': failed_refs[:100],
                         'run_refs_total': len(failed_refs), 'run_refs_truncated': len(failed_refs) > 100})
        queues = self._queues(warnings, specs, issues, now)
        issues = [issue for issue in issues if not (issue['kind'] == 'job' and issue['error'] == 'downstream_attention'
                  and any(other['kind'] in {'delivery', 'feedback'} and other['module'] == issue['module'] for other in issues))]
        for issue in issues:
            label = bridge.sources.get(issue['source_id'], {}).get('name') or {
                'processor': '待办处理', 'messaging': '消息投递', 'feedback': '状态回写', 'automation': '后台服务'}.get(issue['module'], issue['module'])
            issue['title'] = label + '：' + ('投递结果待核对' if issue['next_action'] == 'verify_delivery' else '需要检查运行记录')
            issue['severity'] = 'warning' if issue['status'] == 'unknown' or issue['kind'] in {'service', 'config'} else 'error'
        resources = self.storage.measure(specs, sources)
        for row in resources['sources']:
            row['emitted'] = next(s['emitted'] for s in measured if s['id'] == row['source_id'])
        if not resources['complete']:
            _warning(warnings, 'storage_partial')
        if not auto_coverage['available']:
            _warning(warnings, auto_coverage['status'], 'automation')
        if manual_coverage['unreadable']:
            _warning(warnings, 'console_records_unreadable')
        if auto_coverage['truncated'] or manual_coverage['truncated']:
            _warning(warnings, 'history_window_truncated')
        issues_truncated = any(queue.get('issues_truncated', False) for queue in queues.values())
        if issues_truncated:
            _warning(warnings, 'queue_issues_truncated')
        self._tracked = self._base_paths | {Path(spec['path']) for spec in specs if spec['category'] == 'state'}
        return {'window': {'hours': hours, 'start': start, 'end': now, 'basis': 'episode_started_at',
                           'metric_unit': 'episode', 'action': 'collect', 'duration_basis': 'episode_wall_seconds'},
                'generated_at': now, 'sources': measured, 'summary': summary, 'success_bins': bins,
                'coverage': {'automation': auto_coverage, 'console': manual_coverage,
                             'unmapped_collection_records': unmapped,
                             'scope': 'retained_console_and_automation_records_only'},
                'truncation': {'history': auto_coverage['truncated'] or manual_coverage['truncated'],
                               'resources': resources['truncated'], 'issues': issues_truncated},
                'warnings': warnings, 'issues': issues, 'service': service, 'jobs': live_jobs,
                'queues': queues, 'resources': resources}

    def _queues(self, warnings, specs, issues, now):
        bridge = self.bridge
        result = {'processor': {'pending_groups': None, 'as_of': None, 'status': 'unknown'},
                  'messaging': {'counts': None, 'as_of': None, 'status': 'unknown'},
                  'feedback': {'counts': None, 'as_of': None, 'status': 'unknown'}}
        try:
            cfg = bridge.processor.config()
            value = bridge.processor.core().status(cfg)
            result['processor'].update(pending_groups=value['counts']['pending_groups'], as_of=value['pending_as_of'],
                                       status='last_scan' if value['pending_as_of'] else 'not_computed')
            specs.extend(_specs(bridge.processor.path, cfg, (('storage', 'state_file', 'state'),
                                                          ('storage', 'output_dir', 'output'))))
        except Exception as exc:
            _warning(warnings, type(exc).__name__, 'processor')
        try:
            from messaging import load_config
            from messaging.core import status
            from messaging.feedback import feedback_status
            path = _config_path(bridge, 'messaging')
            if not path.is_file():
                return result
            cfg = load_config(path)
            specs.extend(_specs(path, cfg, (('storage', 'state_file', 'state'), ('storage', 'assets_dir', 'attachments'),
                                           ('feedback', 'state_file', 'state'))))
            value = status(cfg)
            exists = _table_exists(cfg['storage']['state_file'], 'deliveries')
            result['messaging'].update(counts=value['delivery_counts'] if exists else None,
                                       as_of=now if exists else None, status='ok' if exists else 'state_missing')
            rows = _queue_rows(cfg['storage']['state_file'], 'deliveries', 'id,status,error,created_at', ('unknown', 'failed', 'retry')) if exists else []
            result['messaging']['issues_truncated'] = len(rows) > 100
            for row in rows[:100]:
                issues.append(_issue('delivery', 'messaging', identifier=row['id'], error=row.get('error') or row['status'],
                                     last_at=row['created_at'], status=row['status']))
            value = feedback_status(cfg)
            exists = _table_exists(cfg['feedback']['state_file'], 'feedback_requests')
            result['feedback'].update(counts=value['counts'] if exists else None,
                                      as_of=now if exists else None, status='ok' if exists else 'state_missing')
            rows = _queue_rows(cfg['feedback']['state_file'], 'feedback_requests', 'request_id,status,error,updated_at', ('conflict', 'failed', 'retry')) if exists else []
            result['feedback']['issues_truncated'] = len(rows) > 100
            for row in rows[:100]:
                issues.append(_issue('feedback', 'feedback', identifier=row['request_id'],
                                     error=row.get('error') or row['status'], last_at=row['updated_at'], status=row['status']))
        except Exception as exc:
            _warning(warnings, type(exc).__name__, 'messaging')
        return result


def register_overview_routes(app, bridge, jobs, authenticate):
    overview = Overview(bridge, jobs)
    app.state.overview = overview

    @app.get('/api/overview', dependencies=[Depends(authenticate)], summary='同一时间窗内的真实运行、队列与资源概览')
    def snapshot(hours: int = Query(24, ge=1, le=168)):
        return overview.snapshot(hours)
