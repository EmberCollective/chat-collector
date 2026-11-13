"""Optional local controls; the message layer does not depend on this console."""
from pathlib import Path

from fastapi import Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from typing import Literal
import uuid


class TaskAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['complete', 'reopen', 'snooze']
    until: str | None = None


def register_messaging_routes(app, bridge, authenticate):
    def config():
        from messaging import load_config
        path = bridge.root / bridge.settings.get('messaging_config', 'config/messaging.toml')
        if not path.is_file():
            raise HTTPException(404, '尚未配置消息层，请参考 config/messaging.toml')
        return load_config(path)

    def call(callback):
        try:
            return callback(config())
        except (ValueError, FileNotFoundError):
            raise HTTPException(400, '操作未完成，请检查消息层配置、任务编号和素材状态') from None
        except RuntimeError:
            raise HTTPException(409, '消息层正在运行或数据冲突，请稍后重试并检查本地状态') from None

    @app.get('/api/messaging/status', dependencies=[Depends(authenticate)], summary='查看消息层、提醒和投递状态')
    def status():
        from messaging.core import status
        return call(status)

    @app.get('/api/messaging/preview', dependencies=[Depends(authenticate)], summary='本地预览行动与材料，不调用模型或外部平台')
    def preview(limit: int = Query(20, ge=1, le=200)):
        from messaging.core import preview
        return call(lambda cfg: preview(cfg, limit=limit))

    @app.post('/api/messaging/sync', dependencies=[Depends(authenticate)], summary='本地导入待办、保存材料；不发送外部消息')
    def sync():
        from messaging.core import sync
        return call(sync)

    @app.post('/api/messaging/tasks/{task_id}/state', dependencies=[Depends(authenticate)], summary='标记完成、恢复或稍后提醒')
    def state(task_id: str, body: TaskAction):
        from messaging.feedback import request_task_action
        return call(lambda cfg: request_task_action(cfg, task_id, body.action,
                    event_id=uuid.uuid4().hex, actor={'platform':'local','user_id':'console','source':'console'}, until=body.until))

    @app.get('/api/messaging/feedback', dependencies=[Depends(authenticate)], summary='查看卡片操作回写队列与处理结果')
    def feedback():
        from messaging.feedback import feedback_status
        return call(feedback_status)

    @app.get('/api/automation/status', dependencies=[Depends(authenticate)], summary='查看各层自动化、重试与报警状态')
    def automation_status():
        from automation import load_config, status
        path = bridge.root / bridge.settings.get('automation_config', 'config/automation.toml')
        if not path.is_file():
            raise HTTPException(404, '尚未配置自动化，请参考 config/automation.toml')
        return status(load_config(path))

    @app.get('/api/automation/check', dependencies=[Depends(authenticate)], summary='只读检查自动化配置，不启动任务')
    def automation_check():
        from automation import load_config, check
        path = bridge.root / bridge.settings.get('automation_config', 'config/automation.toml')
        if not path.is_file():
            raise HTTPException(404, '尚未配置自动化，请参考 config/automation.toml')
        return check(load_config(path))

    @app.get('/api/messaging/tasks/{task_id}/materials/{resource_id}', dependencies=[Depends(authenticate)], summary='按材料编号下载经校验的本地附件')
    def material(task_id: str, resource_id: str):
        from messaging.core import task_package
        from messaging.package import material_path
        def response(cfg):
            path, name = material_path(task_package(cfg, task_id), resource_id, cfg)
            return FileResponse(path, filename=name, content_disposition_type='attachment')
        return call(response)
