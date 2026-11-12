from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit
import uuid

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bridge import Bridge, atomic_json

ROOT = Path(__file__).resolve().parent
Module = Literal['wechat', 'qq']
TextPath = Annotated[str, Field(min_length=1, max_length=4096)]


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SqlRuleset(Model):
    file: TextPath
    enabled: bool = True
    recent_count: int = Field(default=0, ge=0, le=1000)
    params: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class Filters(Model):
    rulesets: list[SqlRuleset] = Field(default=None, max_length=128)


class Window(Model):
    start: int = Field(ge=0)
    end: int = Field(ge=1)

    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start:
            raise ValueError('结束时间必须晚于开始时间')
        return self


class Preview(Window):
    limit: int = Field(default=20, ge=1, le=200)
    filters: Filters | None = None

    @model_validator(mode='after')
    def bounded(self):
        if self.end-self.start > 7*86400:
            raise ValueError('预览每次最多 7 天；更长范围请分段')
        return self


class SchedulePatch(Model):
    interval_seconds: int = Field(default=None, ge=5, le=86400)
    refresh_interval_seconds: int = Field(default=None, ge=0)
    initial_lookback_seconds: int = Field(default=None, ge=1, le=31*86400)


class SourcePatch(Model):
    encrypted_dir: TextPath = None
    decrypted_dir: TextPath = None
    work_dir: TextPath = None
    media_dir: TextPath = None
    resource_db: TextPath = None
    image_cache_dir: TextPath = None
    key_file: TextPath = None
    refresh: bool = None


class StoragePatch(Model):
    output_dir: TextPath = None


class AttachmentPatch(Model):
    enabled: bool = None
    archive_dir: TextPath = None
    allowed_roots: list[TextPath] = None


class ProcessingPatch(Model):
    intensity: Literal['metadata', 'standard', 'full'] = None


class ConfigPatch(Model):
    processing: ProcessingPatch = Field(default_factory=ProcessingPatch)
    source: SourcePatch = Field(default_factory=SourcePatch)
    storage: StoragePatch = Field(default_factory=StoragePatch)
    attachments: AttachmentPatch = Field(default_factory=AttachmentPatch)
    schedule: SchedulePatch = Field(default_factory=SchedulePatch)
    filters: Filters = Field(default_factory=Filters)


class SaveConfig(Model):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    patch: ConfigPatch


class ScheduleControl(Model):
    enabled: bool


class ProcessorModelPatch(Model):
    base_url: str = Field(default=None, max_length=2048)
    model: str = Field(default=None, max_length=200)
    api_key_env: str = Field(default=None, pattern=r'^[A-Za-z_][A-Za-z0-9_]*$', max_length=200)
    timeout_seconds: int = Field(default=None, ge=1, le=600)
    vision: bool = None
    context_window: int = Field(default=None, ge=4096, le=2000000)
    max_output_tokens: int = Field(default=None, ge=256, le=2000000)


class ProcessorAgentPatch(Model):
    max_turns: int = Field(default=None, ge=1, le=100)
    max_tool_calls: int = Field(default=None, ge=1, le=1000)
    batching: Literal['auto', 'single_conversation'] = None
    auto_compaction: bool = None
    compaction_reserve_tokens: int = Field(default=None, ge=256, le=1999999)
    compaction_keep_recent_tokens: int = Field(default=None, ge=128, le=1999999)
    max_retries: int = Field(default=None, ge=0, le=5)


class ProcessorContextPatch(Model):
    gap_seconds: int = Field(default=None, ge=0, le=86400)
    max_messages: int = Field(default=None, ge=1, le=200)
    max_conversation_chars: int = Field(default=None, ge=1000, le=1000000)
    history_messages: int = Field(default=None, ge=0, le=50)
    timezone: str = Field(default=None, min_length=1, max_length=100)


class ProcessorSchedulePatch(Model):
    interval_seconds: int = Field(default=None, ge=5, le=86400)


class ProcessorLimitsPatch(Model):
    max_input_chars: int = Field(default=None, ge=1000, le=1000000)


class SourceDescriptionsPatch(Model):
    qq: str = Field(default=None, max_length=10000)
    wechat: str = Field(default=None, max_length=10000)


class ProcessorConfigPatch(Model):
    model: ProcessorModelPatch = Field(default_factory=ProcessorModelPatch)
    agent: ProcessorAgentPatch = Field(default_factory=ProcessorAgentPatch)
    context: ProcessorContextPatch = Field(default_factory=ProcessorContextPatch)
    processing: ProcessorLimitsPatch = Field(default_factory=ProcessorLimitsPatch)
    schedule: ProcessorSchedulePatch = Field(default_factory=ProcessorSchedulePatch)
    source_descriptions: SourceDescriptionsPatch = Field(default_factory=SourceDescriptionsPatch)


class SaveProcessorConfig(Model):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    patch: ProcessorConfigPatch


class SaveRules(Model):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    text: str = Field(min_length=1, max_length=1024*1024)


class ProcessorPreview(Model):
    limit: int = Field(default=5, ge=1, le=20)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def window(self):
        if (self.start is None) != (self.end is None):
            raise ValueError('调试起止时间必须一起填写，或一起留空使用默认增量')
        if self.start is not None:
            Preview(start=self.start, end=self.end)
        return self


class Jobs:
    def __init__(self, bridge, runtime):
        self.bridge, self.runtime = bridge, Path(runtime)
        self.directory = self.runtime/'jobs'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.mutex = threading.RLock()
        self.running = {}
        self.jobs = {}
        self.enabled = {name: False for name in bridge.paths}
        self.due = {name: 0.0 for name in bridge.paths}
        self.schedule_errors = {}
        self.stop = threading.Event()
        self.watchers = []
        for path in sorted(self.directory.glob('*/job.json'), key=lambda p: p.stat().st_mtime)[-100:]:
            job = json.loads(path.read_text(encoding='utf-8'))
            if job['status'] == 'running':
                result = path.parent/'result.json'
                job.update(json.loads(result.read_text(encoding='utf-8')) if result.exists() else
                           {'status': 'interrupted', 'error': '上次服务退出时未收到完成结果；需检查断点，不能假定任务已取消'})
                job.pop('result', None)
            self.jobs[job['id']] = job

    def start(self):
        self.scheduler = threading.Thread(target=self.loop, daemon=True)
        self.scheduler.start()

    def close(self):
        self.stop.set()
        self.scheduler.join()
        # Do not kill a collection in the middle of its durable transaction.
        for thread in self.watchers:
            thread.join()

    def loop(self):
        while not self.stop.wait(0.5):
            with self.mutex:
                for name in self.enabled:
                    if self.enabled[name] and name not in self.running and time.monotonic() >= self.due[name]:
                        try:
                            self.submit(name, 'collect', {}, 'schedule')
                            self.schedule_errors.pop(name, None)
                        except Exception as exc:
                            self.schedule_errors[name] = str(exc)
                            self.due[name] = time.monotonic()+60

    def submit(self, name, action, request, origin='manual'):
        with self.mutex:
            if name in self.running:
                raise HTTPException(409, '本模块已有任务运行，请等待完成')
            config = self.bridge.config(name)
            # Probe the same OS lock used by the independent collector CLI.
            try:
                with self.bridge.lock(config['storage']['state_file']):
                    pass
            except RuntimeError:
                raise HTTPException(409, '独立收集进程正在使用本模块，请稍后重试') from None
            identifier = uuid.uuid4().hex
            folder = self.directory/identifier
            folder.mkdir()
            job = {'id': identifier, 'module': name, 'action': action, 'origin': origin,
                   'status': 'running', 'started_at': time.time(), 'finished_at': None}
            atomic_json(folder/'request.json', {'module': name, 'action': action, 'request': request})
            atomic_json(folder/'job.json', job)
            env = dict(os.environ, PYTHONUTF8='1')
            with (folder/'worker.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, '-u', '-B', str(ROOT/'bridge.py'),
                    '--settings', str(self.bridge.settings_path), '--request', str(folder/'request.json'),
                    '--result', str(folder/'result.json')], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            job['pid'] = process.pid
            for old in sorted(self.jobs.values(), key=lambda j: j['started_at'])[:-99]:
                if old['status'] != 'running':
                    self.jobs.pop(old['id'], None)
            self.jobs[identifier] = job
            self.running[name] = identifier
            self.watchers = [thread for thread in self.watchers if thread.is_alive()]
            watcher = threading.Thread(target=self.finish, args=(job, process, folder), daemon=True)
            self.watchers.append(watcher)
            watcher.start()
            return dict(job)

    def finish(self, job, process, folder):
        try:
            process.wait(timeout=120 if job['action'] in {'preview', 'processor_preview'} else None)
            result = json.loads((folder/'result.json').read_text(encoding='utf-8'))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            result = {'status': 'failed', 'error': '预览超过 120 秒，请缩短时间范围或简化筛选条件'}
        except Exception as exc:
            result = {'status': 'failed', 'error': f'任务未正常返回：{type(exc).__name__}；请查看任务日志'}
        with self.mutex:
            job.update({k: v for k, v in result.items() if k != 'result'}, finished_at=time.time())
            try:
                atomic_json(folder/'job.json', job)
            except OSError as exc:
                job['metadata_error'] = f'任务记录未保存：{type(exc).__name__}；请检查磁盘空间'
            self.running.pop(job['module'], None)
            try:
                interval = self.bridge.config(job['module'])['schedule']['interval_seconds']
            except Exception:
                interval = 60
            self.due[job['module']] = time.monotonic()+interval

    def detail(self, identifier):
        with self.mutex:
            if identifier not in self.jobs:
                raise HTTPException(404, '任务不存在或不在最近 100 个启动历史内')
            job = dict(self.jobs[identifier])
        path = self.directory/identifier/'result.json'
        if job['status'] != 'running' and path.is_file():
            job.update(json.loads(path.read_text(encoding='utf-8')))
        log = self.directory/identifier/'worker.log'
        progress = self.directory/identifier/'pi-events.jsonl'
        if progress.is_file():
            with progress.open('rb') as stream:
                stream.seek(max(0, progress.stat().st_size-256000))
                lines = stream.read().decode('utf-8', errors='replace').splitlines()
            events = []
            for line in lines:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass  # A running worker may not have finished the last line yet.
            job['pi_events'] = events[-200:]
        if log.is_file():
            with log.open('rb') as stream:
                stream.seek(max(0, log.stat().st_size-16000))
                job['log_tail'] = stream.read().decode('utf-8', errors='replace')
        return job


def create_app(settings_path=ROOT/'console.toml', runtime=None):
    bridge = Bridge(settings_path)
    jobs = Jobs(bridge, runtime or ROOT/'.runtime')
    token = secrets.token_urlsafe(32)
    header = APIKeyHeader(name='X-Console-Token', auto_error=False)

    def authenticate(value: str | None = Security(header)):
        if not value or not hmac.compare_digest(value, token):
            raise HTTPException(401, '请从面板获取本次会话的 API 凭证')

    @asynccontextmanager
    async def lifespan(app):
        jobs.start()
        yield
        jobs.close()

    app = FastAPI(title='聊天收集调试控制台', version='0.1.0', docs_url=None, redoc_url=None,
                  description='消息收集项目的可选本机调试工具。预览不推进断点；正式采集和重放会写入主项目。', lifespan=lifespan)
    app.state.jobs, app.state.bridge = jobs, bridge

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        host = urlsplit('http://'+request.headers.get('host', '')).hostname
        if host not in {'127.0.0.1', 'localhost'}:
            return JSONResponse({'detail': '只接受本机地址'}, 403)
        origin = request.headers.get('origin')
        expected = str(request.base_url).rstrip('/')
        if origin and origin != expected:
            return JSONResponse({'detail': '不接受跨来源请求'}, 403)
        if request.url.path.startswith('/api/') and request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': '不接受跨站数据访问'}, 403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
        if request.url.path == '/':
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(ValueError)
    @app.exception_handler(FileNotFoundError)
    def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, 400)

    @app.get('/', include_in_schema=False)
    def index():
        return FileResponse(ROOT/'static/index.html')

    app.mount('/static', StaticFiles(directory=ROOT/'static'), name='static')

    @app.get('/docs', include_in_schema=False)
    def docs():
        return get_swagger_ui_html(openapi_url='/openapi.json', title='收集控制台 · OpenAPI',
            swagger_js_url='/static/vendor/swagger-ui-bundle.js', swagger_css_url='/static/vendor/swagger-ui.css',
            swagger_favicon_url='/static/icon.svg', swagger_ui_parameters={'persistAuthorization': False})

    @app.get('/api/session', include_in_schema=False)
    def session():
        return {'token': token}

    @app.get('/api/status', dependencies=[Depends(authenticate)], summary='查看两模块状态')
    def status():
        modules = {}
        with jobs.mutex:
            for name in bridge.paths:
                try:
                    data = bridge.state(name)
                except Exception as exc:
                    data = {'state_error': f'{type(exc).__name__}: {exc}'}
                modules[name] = dict(data, scheduler_enabled=jobs.enabled[name],
                    running_job=jobs.running.get(name), next_run_in=max(0, round(jobs.due[name]-time.monotonic())) if jobs.enabled[name] else None,
                    scheduler_error=jobs.schedule_errors.get(name))
            recent = [dict(job) for job in sorted(jobs.jobs.values(), key=lambda j: j['started_at'], reverse=True)[:30]]
        return {'modules': modules, 'jobs': recent, 'collector_root': str(bridge.root),
                'external_runner': bridge.external_runner(),
                'scheduler_scope': '只管理本控制台启动的定时任务；独立 CLI 不受暂停按钮控制'}

    @app.get('/api/modules/{module}/config', dependencies=[Depends(authenticate)], summary='读取配置与版本')
    def config(module: Module):
        return bridge.read_config(module)

    @app.patch('/api/modules/{module}/config', dependencies=[Depends(authenticate)], summary='校验、备份并保存配置')
    def save(module: Module, body: SaveConfig):
        with jobs.mutex:
            if module in jobs.running:
                raise HTTPException(409, '任务运行中不能修改配置')
            try:
                with bridge.lock(bridge.config(module)['storage']['state_file']):
                    result = bridge.write_config(module, body.revision, body.patch.model_dump(exclude_unset=True), jobs.runtime/'config-backups')
                jobs.due[module] = time.monotonic()+bridge.config(module)['schedule']['interval_seconds']
                return result
            except (FileExistsError, RuntimeError) as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post('/api/modules/{module}/schedule', dependencies=[Depends(authenticate)], summary='暂停或恢复本控制台定时采集')
    def schedule(module: Module, body: ScheduleControl):
        with jobs.mutex:
            if body.enabled and bridge.external_runner():
                raise HTTPException(409, '原独立定时采集仍在运行或无法核验；请先运行 tools/handover.cmd 完成切换')
            jobs.enabled[module] = body.enabled
            jobs.due[module] = time.monotonic()
        return {'enabled': body.enabled, 'running_job_continues': module in jobs.running}

    @app.post('/api/modules/{module}/collect', status_code=202, dependencies=[Depends(authenticate)], summary='执行一轮正式增量采集')
    def collect(module: Module):
        return jobs.submit(module, 'collect', {})

    @app.post('/api/modules/{module}/replay', status_code=202, dependencies=[Depends(authenticate)], summary='重放时间段，不移动定时断点')
    def replay(module: Module, body: Window):
        return jobs.submit(module, 'replay', body.model_dump())

    @app.post('/api/modules/{module}/preview', status_code=202, dependencies=[Depends(authenticate)], summary='只读预览筛选结果')
    def preview(module: Module, body: Preview):
        return jobs.submit(module, 'preview', body.model_dump(exclude_none=True))

    @app.get('/api/jobs/{identifier}', dependencies=[Depends(authenticate)], summary='查看任务结果及日志')
    def job(identifier: str):
        return jobs.detail(identifier)

    @app.get('/api/processor/config', dependencies=[Depends(authenticate)], summary='读取处理配置，不含密钥值')
    def processor_config():
        return bridge.processor.read_config()

    def processor_save(callback):
        with jobs.mutex:
            if 'processor' in jobs.running:
                raise HTTPException(409, '处理任务运行中，请等待完成后保存')
            try:
                with bridge.lock(bridge.processor.config()['storage']['state_file']):
                    return callback()
            except (FileExistsError, RuntimeError) as exc:
                raise HTTPException(409, str(exc)) from None

    @app.patch('/api/processor/config', dependencies=[Depends(authenticate)], summary='校验、备份并保存处理配置')
    def save_processor_config(body: SaveProcessorConfig):
        return processor_save(lambda: bridge.processor.write_config(body.revision,
            body.patch.model_dump(exclude_unset=True), jobs.runtime/'config-backups'))

    @app.get('/api/processor/rules', dependencies=[Depends(authenticate)], summary='读取本地 Markdown 处理规则及版本')
    def processor_rules():
        return bridge.processor.read_rules()

    @app.put('/api/processor/rules', dependencies=[Depends(authenticate)], summary='备份并保存处理规则，冲突时拒绝覆盖')
    def save_processor_rules(body: SaveRules):
        return processor_save(lambda: bridge.processor.write_rules(body.revision, body.text,
            jobs.runtime/'rules-backups'))

    @app.get('/api/processor/status', dependencies=[Depends(authenticate)], summary='只读查看处理状态')
    def processor_status():
        result = bridge.processor.core().status(bridge.processor.config())
        with jobs.mutex:
            return dict(result, running_job=jobs.running.get('processor'), scheduler_enabled=False)

    @app.get('/api/processor/tasks', dependencies=[Depends(authenticate)], summary='查看结构化待办及来源证据')
    def processor_tasks(limit: int = Query(20, ge=1, le=100)):
        return {'tasks': bridge.processor.core().list_tasks(bridge.processor.config(), limit=limit)}

    @app.get('/api/processor/reviews', dependencies=[Depends(authenticate)], summary='查看分组结果与附件读取状态')
    def processor_reviews(limit: int = Query(20, ge=1, le=100)):
        return {'groups': bridge.processor.core().list_group_results(bridge.processor.config(), limit=limit)}

    @app.post('/api/processor/preview', status_code=202, dependencies=[Depends(authenticate)], summary='只读预览分组上下文，不调用模型')
    def processor_preview(body: ProcessorPreview):
        return jobs.submit('processor', 'processor_preview', body.model_dump())

    @app.post('/api/processor/run', status_code=202, dependencies=[Depends(authenticate)], summary='调用已配置模型，执行一轮待办处理')
    def processor_run(body: ProcessorPreview | None = None):
        model = bridge.processor.core().status(bridge.processor.config())['model']
        if not model['configured']:
            raise HTTPException(400, '模型尚未配置完整，请先填写 API 地址、模型名称和本机凭据')
        return jobs.submit('processor', 'process', body.model_dump(exclude_none=True) if body else {})

    @app.get('/api/modules/{module}/messages', dependencies=[Depends(authenticate)], summary='查看已采集消息的最新修订')
    def messages(module: Module, limit: int = Query(20, ge=1, le=100), keyword: str = Query('', max_length=500), conversation: str = Query('', max_length=200)):
        return {'messages': bridge.messages(module, limit, keyword, conversation)}

    @app.get('/api/modules/{module}/source', dependencies=[Depends(authenticate)], summary='按消息标识和修订读取本地原消息证据')
    def source(module: Module, message_key: str = Query(min_length=1, max_length=256), revision: int = Query(ge=1)):
        return bridge.message_by_key(module, message_key, revision)

    @app.get('/api/modules/{module}/messages/{seq}/assets/{filename}', dependencies=[Depends(authenticate)], summary='读取此消息的归档附件')
    def asset(module: Module, seq: int, filename: str, inline: bool = False):
        path = bridge.asset(module, seq, filename)
        types = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp'}
        safe = inline and path.suffix in types
        return FileResponse(path, media_type=types[path.suffix] if safe else 'application/octet-stream',
                            filename=path.name, content_disposition_type='inline' if safe else 'attachment')

    return app


if __name__ == '__main__':
    import uvicorn
    app = create_app()
    port = int(app.state.bridge.settings.get('port', 10400))
    with app.state.bridge.lock(ROOT/'.runtime/console-instance'):
        print(f'面板：http://127.0.0.1:{port}  OpenAPI：http://127.0.0.1:{port}/docs', flush=True)
        uvicorn.run(app, host='127.0.0.1', port=port, access_log=False)
