"""Record processor CLI and background rounds without a dependency on the console."""
from functools import wraps
import json
import os
from pathlib import Path
import time
import uuid
import warnings

import psutil


def _write_metadata(folder, value):
    temporary = folder / 'job.json.tmp'
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    os.replace(temporary, folder / 'job.json')


def record_round(run):
    @wraps(run)
    def recorded(config, *args, **kwargs):
        if config.get('_progress_file'):
            return run(config, *args, **kwargs)
        directory = Path(config['storage']['state_file']).resolve().parent / 'pi-runs'
        folder = directory / uuid.uuid4().hex
        folder.mkdir(parents=True)
        origin = config.get('_workflow_origin')
        metadata = {'id': folder.name, 'module': 'processor', 'action': 'process',
                    'origin': origin if origin in {'manual', 'schedule', 'cli'} else 'processor',
                    'status': 'running', 'started_at': time.time(), 'finished_at': None,
                    'pid': os.getpid(), 'create_time': psutil.Process().create_time()}
        settings = dict(config, _progress_file=str(folder / 'pi-events.jsonl'))
        try:
            _write_metadata(folder, metadata)
            result = run(settings, *args, **kwargs)
            metadata['status'] = 'succeeded'
            metadata['summary'] = {key: result[key] for key in ('processed_groups', 'pending_groups', 'tasks_upserted')
                                   if isinstance(result, dict) and type(result.get(key)) is int}
            return result
        except BaseException:
            metadata.update(status='failed', error='处理未完成，请查看工作流失败节点与处理程序日志')
            raise
        finally:
            try:
                if (folder / 'pi-workflow.jsonl').is_file():
                    metadata['finished_at'] = time.time()
                    _write_metadata(folder, metadata)
                else:
                    # Idle polling rounds have no model activity and must not accumulate history stubs.
                    (folder / 'job.json').unlink(missing_ok=True)
                    (folder / 'job.json.tmp').unlink(missing_ok=True)
                    folder.rmdir()
            except OSError:
                warnings.warn('Pi 运行元数据未能保存或清理，请检查磁盘空间', RuntimeWarning, stacklevel=2)
    return recorded
