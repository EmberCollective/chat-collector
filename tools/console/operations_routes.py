"""Optional local controls for the independent automation and delivery packages."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import time
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
import tomlkit


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class JobSettings(StrictModel):
    enabled: bool
    interval_seconds: int = Field(ge=1, le=86400)
    max_attempts: int = Field(ge=1, le=20)
    backoff_seconds: int = Field(ge=1, le=86400)
    max_backoff_seconds: int = Field(ge=1, le=86400)


class ListenerSettings(StrictModel):
    enabled: bool


class AlertSettings(StrictModel):
    enabled: bool
    target_id: str = Field(max_length=200)
    cooldown_seconds: int = Field(ge=1, le=2592000)
    recovery_enabled: bool


class AutomationSettings(StrictModel):
    enabled: bool
    jobs: dict[Literal['processor', 'messaging', 'feedback'], JobSettings]
    listener: ListenerSettings
    alerts: AlertSettings


class SaveSettings(StrictModel):
    version: dict[Literal['automation', 'messaging'], str]
    automation: AutomationSettings
    messaging: dict


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def errors():
    try:
        yield
    except FileExistsError:
        raise HTTPException(409, '配置已变化，请重新加载后再保存') from None
    except FileNotFoundError:
        raise HTTPException(404, '配置或本地记录不存在') from None
    except (ValueError, TypeError):
        raise HTTPException(400, '配置或操作无效，请检查目标、凭据环境变量和参数范围') from None
    except RuntimeError:
        raise HTTPException(409, '操作正在进行或存在冲突，请刷新运行状态后重试') from None
    except OSError:
        raise HTTPException(503, '本地文件或进程操作未完成，请检查权限与存储状态') from None


def register_operations_routes(app, bridge, authenticate):
    def paths():
        return tuple(bridge.root / bridge.settings.get(name + '_config', 'config/' + name + '.toml')
                     for name in ('automation', 'messaging'))

    def read():
        from automation import load_config
        from messaging.management import read_settings
        auto_path, msg_path = paths()
        raw = auto_path.read_bytes()
        cfg = load_config(auto_path)
        msg = read_settings(msg_path)
        auto = {'enabled': cfg['enabled'],
                'jobs': {name: {key: cfg['jobs'][name][key] for key in JobSettings.model_fields}
                         for name in ('processor', 'messaging', 'feedback')},
                'listener': {'enabled': cfg['listener']['enabled']},
                'alerts': {key: cfg['alerts'][key] for key in AlertSettings.model_fields}}
        return {'version': {'automation': _hash(raw), 'messaging': msg.pop('version')},
                'automation': auto, 'messaging': msg}

    @app.get('/api/operations/config', dependencies=[Depends(authenticate)])
    def config():
        with errors():
            return read()

    @app.put('/api/operations/config', dependencies=[Depends(authenticate)])
    def save(body: SaveSettings):
        from automation import load_config
        from automation import control
        from messaging.management import save_settings
        auto_path, msg_path = paths()
        with errors(), bridge.lock(app.state.jobs.runtime / 'operations-config'):
            old_auto, old_msg = auto_path.read_bytes(), msg_path.read_bytes()
            if body.version != {'automation': _hash(old_auto), 'messaging': _hash(old_msg)}:
                raise FileExistsError('stale_configuration')
            old_config = load_config(auto_path)
            running = control.status(old_config)['running']
            document = tomlkit.parse(old_auto.decode('utf-8'))
            patch = body.automation.model_dump()
            document['enabled'] = patch['enabled']
            for name, values in patch['jobs'].items():
                document.setdefault('jobs', {}).setdefault(name, {}).update(values)
            for section in ('listener', 'alerts'):
                document.setdefault(section, {}).update(patch[section])
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', suffix='.toml', dir=auto_path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(tomlkit.dumps(document))
                    stream.flush()
                    os.fsync(stream.fileno())
                load_config(temporary)
                backup = app.state.jobs.runtime / 'config-backups' / ('automation-' + str(time.time_ns()) + '.toml')
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(old_auto)
                backup.with_name(backup.name.replace('automation-', 'messaging-', 1)).write_bytes(old_msg)
                payload = {k: v for k, v in body.messaging.items() if k not in {'schemas', 'version'}}
                saved = save_settings(msg_path, payload, body.version['messaging'])
                try:
                    if auto_path.read_bytes() != old_auto:
                        raise FileExistsError('automation_changed_during_save')
                    os.replace(temporary, auto_path)
                except Exception:
                    # A failed second file replacement must not leave this operation half-applied.
                    with bridge.lock(msg_path, timeout=5):
                        if _hash(msg_path.read_bytes()) == saved['version']:
                            rollback = msg_path.with_suffix('.rollback.tmp')
                            rollback.write_bytes(old_msg)
                            os.replace(rollback, msg_path)
                    raise
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            cfg = load_config(auto_path)
            operation = (control.restart(cfg) if cfg['enabled'] else control.stop(cfg)) if running else None
            return {**read(), 'operation': operation, 'saved': True}

    @app.get('/api/operations/status', dependencies=[Depends(authenticate)])
    def status():
        from automation import load_config, status, check, control
        from messaging import load_config as load_message, status as message_status
        from messaging.feedback import feedback_status
        from messaging.management import test_history
        auto_path, msg_path = paths()
        with errors():
            auto, msg = load_config(auto_path), load_message(msg_path)
            return {'service': control.status(auto), 'automation': status(auto), 'checks': check(auto),
                    'messaging': message_status(msg), 'feedback': feedback_status(msg), 'tests': test_history(msg_path)}

    @app.post('/api/operations/service/{action}', dependencies=[Depends(authenticate)])
    def service(action: Literal['start', 'stop', 'restart']):
        from automation import load_config, control
        with errors():
            return getattr(control, action)(load_config(paths()[0]))

    @app.post('/api/operations/jobs/{name}/{action}', dependencies=[Depends(authenticate)])
    def job(name: Literal['processor', 'messaging', 'feedback', 'listener'], action: Literal['once', 'resume']):
        from automation import load_config, resume, control
        with errors():
            cfg = load_config(paths()[0])
            return control.trigger(cfg, name) if action == 'once' else resume(cfg, name)

    @app.post('/api/operations/targets/{target_id}/test', dependencies=[Depends(authenticate)])
    def test(target_id: str):
        from messaging.management import send_test
        with errors():
            return send_test(paths()[1], target_id)

    @app.post('/api/operations/deliveries/{identifier}/{action}', dependencies=[Depends(authenticate)])
    def delivery(identifier: str, action: Literal['retry', 'delivered']):
        from messaging import load_config, resolve_delivery
        with errors():
            return resolve_delivery(load_config(paths()[1]), identifier, action)

    @app.post('/api/operations/feedback/{identifier}/retry', dependencies=[Depends(authenticate)])
    def feedback(identifier: str):
        from messaging import load_config, retry_feedback
        with errors():
            return retry_feedback(load_config(paths()[1]), identifier)
