"""One-way integration: the collector never imports this project."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import datetime
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import tomllib
import uuid

import tomlkit
import psutil
from processing_bridge import ProcessingBridge


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


class Bridge:
    def __init__(self, settings_path):
        self.settings_path = Path(settings_path).resolve()
        self.settings = tomllib.loads(self.settings_path.read_text(encoding='utf-8'))
        self.root = (self.settings_path.parent / self.settings['collector_root']).resolve(strict=True)
        if not (self.root / 'collector/__main__.py').is_file():
            raise ValueError('collector_root 不是有效的收集项目目录')
        sys.path[:0] = [str(self.root), str(self.root / 'runtime-deps')]
        from collector.config import load_config
        from collector.__main__ import run_round, InstanceLock
        from collector.filters import scan_filtered, load_rulesets
        self.scan_filtered, self.load_rulesets = scan_filtered, load_rulesets
        self.load = load_config
        self.run_round, self.lock = run_round, InstanceLock
        self.processor = ProcessingBridge(self.root, self.settings)
        self.paths = {name: (self.root / path).resolve(strict=True)
                      for name, path in self.settings['modules'].items()}
        if set(self.paths) != {'wechat', 'qq'}:
            raise ValueError('modules 必须包含 wechat 和 qq')
        for name in self.paths:
            if self.config(name)['platform'] != name:
                raise ValueError('模块与配置平台不匹配')

    def config(self, name):
        if name == 'processor':
            return self.processor.config()
        return self.load(self.paths[name])

    def external_runner(self):
        marker = self.root/'data/collector-process.json'
        if not marker.is_file():
            return None
        try:
            record = json.loads(marker.read_text(encoding='utf-8-sig'))
            process = psutil.Process(int(record['pid']))
            expected = datetime.datetime.fromisoformat(record['started_utc'].replace('Z', '+00:00')).timestamp()
            if abs(process.create_time()-expected) >= 5:
                return None
            command = ' '.join(process.cmdline())
            if '-m collector run' not in command or str(self.root) not in command or not all(str(path) in command for path in self.paths.values()):
                return None
            return {'pid': process.pid, 'verified': True, 'message': '原独立定时采集仍在运行；请先完成切换，避免双重调度'}
        except psutil.NoSuchProcess:
            return None
        except (psutil.AccessDenied, ValueError, KeyError, OSError) as exc:
            return {'verified': False, 'message': '无法核验原定时采集状态：'+type(exc).__name__}

    def read_config(self, name):
        raw = self.paths[name].read_bytes()
        config = tomllib.loads(raw.decode('utf-8'))
        config.setdefault('processing', {}).setdefault('intensity', 'full')
        config.setdefault('schedule', {}).setdefault('interval_seconds', 60)
        config['schedule'].setdefault('initial_lookback_seconds', 86400)
        config['schedule'].setdefault('refresh_interval_seconds', 0)
        return {'revision': hashlib.sha256(raw).hexdigest(),
                'config': config, 'file': str(self.paths[name])}

    def write_config(self, name, revision, patch, backup_dir):
        path = self.paths[name]
        before = path.read_bytes()
        if hashlib.sha256(before).hexdigest() != revision:
            raise FileExistsError('配置已被修改，请重新加载后再保存')
        document = tomlkit.parse(before.decode('utf-8'))
        for section, values in patch.items():
            for key, value in values.items():
                document.setdefault(section, tomlkit.table())[key] = value
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=path.parent, suffix='.toml', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(tomlkit.dumps(document))
            proposed = self.load(temporary)
            self.load_rulesets(proposed)
            old = self.config(name)
            # The UI must not reset identity/dedup state or redirect output over inputs.
            if proposed['source']['account_id'] != old['source']['account_id'] or proposed['storage']['state_file'] != old['storage']['state_file']:
                raise ValueError('账号和状态库不能通过面板迁移')
            inputs = [Path(proposed['source'][k]).resolve() for k in ('encrypted_dir','media_dir','image_cache_dir') if proposed['source'].get(k)]
            destinations = [Path(proposed['source']['decrypted_dir']).resolve(), Path(proposed['storage']['output_dir']).resolve()]
            if proposed['attachments'].get('enabled'):
                destinations.append(Path(proposed['attachments']['archive_dir']).resolve())
            for dest in destinations:
                if any(dest == p or dest in p.parents or p in dest.parents for p in inputs):
                    raise ValueError('输出目录不能与原始数据目录重合或相互包含')
                protected = [Path(proposed['storage']['state_file']).resolve(), Path(proposed['source']['key_file']).resolve()]
                if any(dest == p or dest in p.parents for p in protected):
                    raise ValueError('输出目录不能包含状态库或密钥文件')
            if len(set(destinations)) != len(destinations) or any(a in b.parents for a in destinations for b in destinations if a != b):
                raise ValueError('明文库、批次和附件目录必须彼此独立')
            for key in ('encrypted_dir', 'media_dir', 'key_file'):
                if proposed['source'].get(key) and not Path(proposed['source'][key]).exists():
                    raise ValueError('配置路径不存在：' + key)
            if path.read_bytes() != before:
                raise FileExistsError('配置被其他程序修改，请重新加载')
            backup = Path(backup_dir) / f'{name}-{time.time_ns()}.toml'
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(before)
            os.replace(temporary, path)
            return {**self.read_config(name), 'backup': str(backup)}
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def connect_state(self, name):
        path = Path(self.config(name)['storage']['state_file']).resolve()
        if not path.is_file():
            return None
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
        connection.execute('PRAGMA query_only=ON')
        return connection

    def state(self, name):
        config = self.config(name)
        connection = self.connect_state(name)
        result = {'checkpoint': None, 'messages': 0, 'events': 0, 'pending_events': 0}
        if connection:
            try:
                row = connection.execute("SELECT value FROM settings WHERE key='window_end'").fetchone()
                result.update(checkpoint=int(row[0]) if row else None,
                              messages=connection.execute('SELECT count(*) FROM seen').fetchone()[0],
                              events=connection.execute('SELECT count(*) FROM outbox').fetchone()[0],
                              pending_events=connection.execute('SELECT count(*) FROM outbox WHERE exported=0').fetchone()[0])
            finally:
                connection.close()
        snapshots = [p for p in Path(config['source']['decrypted_dir']).glob('*.db') if p.is_file()]
        result.update(account=config['source']['account_id'], interval_seconds=config['schedule']['interval_seconds'],
                      intensity=config['processing']['intensity'], attachments_enabled=config['attachments'].get('enabled', False),
                      refresh_interval_seconds=config['schedule']['refresh_interval_seconds'],
                      snapshot_updated_at=max((p.stat().st_mtime for p in snapshots), default=None),
                      snapshot_files=len(snapshots), config_file=str(self.paths[name]))
        return result

    def messages(self, name, limit=20, keyword='', conversation=''):
        connection = self.connect_state(name)
        if connection is None:
            return []
        try:
            rows = connection.execute('''SELECT o.seq,o.body FROM outbox o JOIN seen s
                ON json_extract(o.body,'$.message_key')=s.message_key
                AND json_extract(o.body,'$.revision')=s.revision
                WHERE (?='' OR instr(json_extract(o.body,'$.message.text'),?)>0)
                  AND (?='' OR json_extract(o.body,'$.message.conversation_id')=?)
                ORDER BY json_extract(o.body,'$.message.sent_at') DESC,o.seq DESC LIMIT ?''',
                (keyword, keyword, conversation, conversation, limit)).fetchall()
            return [dict(seq=seq, **json.loads(body)) for seq, body in rows]
        finally:
            connection.close()

    def message_by_key(self, name, message_key, revision):
        connection = self.connect_state(name)
        if connection is None:
            raise FileNotFoundError('此模块尚无本地消息证据')
        try:
            row = connection.execute("SELECT seq,body FROM outbox WHERE json_extract(body,'$.message_key')=? AND json_extract(body,'$.revision')=? LIMIT 1",
                                     (message_key, revision)).fetchone()
            if not row:
                raise FileNotFoundError('原消息的此修订未保留在当前采集库；待办中的来源快照仍可查看')
            return dict(seq=row[0], **json.loads(row[1]))
        finally:
            connection.close()

    def asset(self, name, seq, filename):
        if not re.fullmatch(r'[0-9a-f]{64}\.[a-z0-9]{1,12}', filename):
            raise ValueError('附件名称无效')
        connection = self.connect_state(name)
        if connection is None:
            raise FileNotFoundError('尚无消息')
        try:
            row = connection.execute('SELECT body FROM outbox WHERE seq=?', (seq,)).fetchone()
        finally:
            connection.close()
        if not row:
            raise FileNotFoundError('消息不存在')
        root = Path(self.config(name)['attachments']['archive_dir']).resolve()
        def candidates(value):
            if isinstance(value, dict):
                if value.get('archive_path'):
                    yield Path(value['archive_path']).resolve()
                for item in value.values():
                    yield from candidates(item)
            elif isinstance(value, list):
                for item in value:
                    yield from candidates(item)
        for path in candidates(json.loads(row[0])['message'].get('attachments', [])):
            if path.name == filename and path.is_relative_to(root) and path.is_file():
                return path
        raise FileNotFoundError('附件未归档、已移动，或不属于此消息/当前归档目录')

    def context_window(self, request, *, process=False):
        from collector.store import digest
        start, end = request['start'], request['end']
        if type(start) is not int or type(end) is not int or not 0 <= start < end or end-start > 7*86400:
            raise ValueError('上下文调试需要有效的起止时间，每次最多 7 天')
        config = self.processor.config()
        collected_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # Debug runs keep their own inputs and output; preview files are disposable.
        location = (nullcontext(Path(config['storage']['state_file']).parent/'debug'/uuid.uuid4().hex)
                    if process else tempfile.TemporaryDirectory(prefix='collector-context-preview-'))
        with location as folder:
            folder = Path(folder)
            incoming = folder/'incoming'
            incoming.mkdir(parents=True)
            with (incoming/'preview.jsonl').open('w', encoding='utf-8') as stream:
                for module in self.paths:
                    source = self.config(module)
                    with self.lock(source['storage']['state_file']):
                        for message in self.scan_filtered(source, start, end):
                            rules = message.pop('matched_rules', [])
                            key = digest([str(message[k]) for k in ('platform','account_id','conversation_id','message_id')])
                            event = dict(schema_version=1, operation='upsert', event_id=digest(['preview',key]),
                                message_key=key, revision=1, collected_at=collected_at, matched_rules=rules,
                                window={'start_inclusive':start,'end_exclusive':end}, message=message)
                            stream.write(json.dumps(event, ensure_ascii=False)+'\n')
            config['input'].update(batches_dir=str(incoming), start_at=start, refresh_collectors=False)
            config['_task_search_state_file'] = config['storage']['state_file']
            config['storage'].update(state_file=str(folder/'state.db'), output_dir=str(folder/'todos'))
            if process:
                atomic_json(folder/'request.json', {'start':start,'end':end,'mode':'debug'})
                try:
                    result = self.processor.core().run_round(config)
                except Exception as exc:
                    raise RuntimeError(f'调试处理未完成；输入和已完成结果保留在 {folder}：{exc}') from exc
                result.update(debug_window={'start':start,'end':end}, debug_output_dir=str(folder),
                    tasks=[dict(task, debug_run=str(folder)) for task in self.processor.core().list_tasks(config, limit=1000)],
                    checkpoint_advanced=False, formal_state_changed=False)
                atomic_json(folder/'result.json', result)
                return result
            result = self.processor.core().preview(config, limit=request.get('limit',5))
        result.update(source_descriptions=config['source_descriptions'],
            debug_window={'start':start,'end':end}, snapshot_only=True, checkpoint_advanced=False)
        result['warnings'].append('手动时间预览：按当前 SQL 查询已解密快照，不刷新数据库、不归档附件、不改变默认起点或正式断点，不调用模型。范围外的必要前文作为历史上下文保留。')
        return result

    def execute(self, name, action, request):
        if name == 'processor':
            if action in {'processor_preview', 'process'} and request.get('start') is not None:
                return self.context_window(request, process=action == 'process')
            return self.processor.execute(action, request)
        config = self.config(name)
        if action == 'collect':
            return self.run_round(config)
        if action == 'replay':
            return self.run_round(config, start=request['start'], end=request['end'])
        # Only the adapter's read-only scan is used. No refresh, archive or Store.
        if request.get('filters') is not None:
            config['filters'].update(request['filters'])
        with self.lock(config['storage']['state_file']):
            count = 0
            def counted():
                nonlocal count
                for message in self.scan_filtered(config, request['start'], request['end']):
                    count += 1
                    yield message
            messages = heapq.nlargest(request.get('limit', 20), counted(), key=lambda m: (m['sent_at'], m['message_id']))
        return {'matched': count, 'messages': messages,
                'start': request['start'], 'end': request['end'],
                'rulesets': config.get('filters',{}).get('rulesets',[]),
                'snapshot_only': True, 'checkpoint_advanced': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--settings', required=True)
    parser.add_argument('--request', required=True)
    parser.add_argument('--result', required=True)
    args = parser.parse_args()
    import logging
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger('collector.vendor.wechat.decrypt').setLevel(logging.ERROR)
    try:
        request = json.loads(Path(args.request).read_text(encoding='utf-8'))
        bridge = Bridge(args.settings)
        bridge.processor.progress_file = Path(args.result).with_name('pi-events.jsonl')
        result = bridge.execute(request['module'], request['action'], request['request'])
        atomic_json(args.result, {'status': 'succeeded', 'result': result})
    except Exception as exc:
        atomic_json(args.result, {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'})
        raise SystemExit(1)
