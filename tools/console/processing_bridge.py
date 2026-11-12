"""Optional, one-way adapter for the independent processing core."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import time

import tomlkit


EDITABLE = {
    'model': {'base_url', 'model', 'api_key_env', 'timeout_seconds', 'vision', 'context_window', 'max_output_tokens'},
    'agent': {'max_turns', 'max_tool_calls', 'batching', 'auto_compaction', 'compaction_reserve_tokens',
              'compaction_keep_recent_tokens', 'max_retries'},
    'context': {'gap_seconds', 'max_messages', 'history_messages', 'timezone', 'max_conversation_chars'},
    'processing': {'max_input_chars'},
    'schedule': {'interval_seconds'},
    'source_descriptions': {'qq', 'wechat'},
}


class ProcessingBridge:
    def __init__(self, root, settings):
        self.path = (root / settings.get('processor_config', 'config/processor.toml')).resolve()
        self.progress_file = None

    @staticmethod
    def core():
        import processor
        return processor

    def config(self):
        config = self.core().load_config(self.path)
        if self.progress_file:
            config['_progress_file'] = str(self.progress_file)
        return config

    def read_config(self):
        raw = self.path.read_bytes()
        config = self.config()
        public = {section: {key: value for key, value in config.get(section, {}).items() if key in keys}
                  for section, keys in EDITABLE.items()}
        # Paths are informative only. Never return an API key or arbitrary TOML fields.
        public['paths'] = {key: str(config[section].get(field, '')) for key, section, field in (
            ('input', 'input', 'batches_dir'), ('state', 'storage', 'state_file'),
            ('output', 'storage', 'output_dir'), ('rules', 'rules', 'file'),
            ('api_key_file', 'model', 'api_key_file'))}
        public['input'] = {'start_at': config['input'].get('start_at')}
        return {'revision': hashlib.sha256(raw).hexdigest(), 'file': str(self.path), 'config': public}

    def read_rules(self):
        path = Path(self.config()['rules']['file'])
        if path.suffix.lower() != '.md':
            raise ValueError('规则编辑器只支持配置指定的 Markdown 文件')
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('规则文件超过 1 MiB，请在本地编辑器维护')
        raw = path.read_bytes()
        return {'revision': hashlib.sha256(raw).hexdigest(), 'file': str(path), 'text': raw.decode('utf-8')}

    @staticmethod
    def replace(path, before, text, backup_dir, validate=None):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=path.parent,
                                             suffix=path.suffix, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(text)
            if validate:
                validate(temporary)
            if path.read_bytes() != before:
                raise FileExistsError('文件已被其他程序修改，请重新加载后保存')
            backup = Path(backup_dir) / f'processor-{path.stem}-{time.time_ns()}{path.suffix}'
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(before)
            os.replace(temporary, path)
            return str(backup)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def write_config(self, revision, patch, backup_dir):
        before = self.path.read_bytes()
        if hashlib.sha256(before).hexdigest() != revision:
            raise FileExistsError('处理配置已变化，请重新加载后保存')
        document = tomlkit.parse(before.decode('utf-8'))
        for section, values in patch.items():
            if section not in EDITABLE or set(values) - EDITABLE[section]:
                raise ValueError('此配置项不能通过调试台修改')
            for key, value in values.items():
                document.setdefault(section, tomlkit.table())[key] = value
        backup = self.replace(self.path, before, tomlkit.dumps(document), backup_dir, self.core().load_config)
        return {**self.read_config(), 'backup': backup}

    def write_rules(self, revision, text, backup_dir):
        rules = self.read_rules()
        if rules['revision'] != revision:
            raise FileExistsError('处理规则已变化，请重新加载后保存；当前编辑内容仍保留在页面')
        raw = text.encode('utf-8')
        if not text.strip() or len(raw) > 1024 * 1024:
            raise ValueError('规则不能为空，且不能超过 1 MiB')
        path = Path(rules['file'])
        backup = self.replace(path, rules['text'].encode('utf-8'), text, backup_dir)
        return {**self.read_rules(), 'backup': backup}

    def execute(self, action, request):
        core, config = self.core(), self.config()
        if action == 'processor_preview':
            return dict(core.preview(config, limit=request.get('limit', 5)),
                        source_descriptions=config['source_descriptions'])
        if action == 'process':
            return core.run_round(config)
        raise ValueError('未知处理操作')
