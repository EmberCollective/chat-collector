"""Explicit local plugin registration; message contents never select executable code."""
from __future__ import annotations

import importlib
from copy import deepcopy
from pathlib import Path
import re


def configure_providers(settings, base):
    entries = settings.setdefault('providers', {})
    if not isinstance(entries, dict):
        raise ValueError('attachments.providers must be a platform registry')
    for platform, entry in entries.items():
        if not isinstance(platform, str) or not platform:
            raise ValueError('Attachment provider platform must be nonempty text')
        if (not isinstance(entry, dict) or set(entry) != {'factory', 'config'}
                or not isinstance(entry['factory'], str)
                or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*', entry['factory'])
                or not isinstance(entry['config'], str) or not entry['config']):
            raise ValueError('Attachment provider needs factory="module:callable" and a config path')
        if platform in settings.get('collector_configs', {}):
            raise ValueError('Attachment provider registered twice for the same platform')
        entry['config'] = str((Path(base) / entry['config']).resolve())


class AttachmentProviders:
    def __init__(self, config):
        settings = deepcopy(config.get('attachments', {}))
        base = Path(config['_config_path']).resolve().parent if config.get('_config_path') else Path.cwd()
        configure_providers(settings, base)
        self.entries = dict(settings.get('providers', {}))
        self.entries.update({platform: {'factory': 'providers.platforms.attachments:CollectorAttachmentProvider', 'config': path}
                             for platform, path in settings.get('collector_configs', {}).items()})
        self.paths = self.entries
        self.cache = Path(settings.get('cache_dir') or Path(config['storage']['state_file']).parent / 'attachment-cache')
        self.instances = {}

    def resolve(self, event, index):
        platform = event['message'].get('platform')
        entry = self.entries.get(platform)
        if entry is None:
            raise ValueError('attachment_provider_not_configured')
        if type(index) is not int or not 0 <= index < len(event['message'].get('attachments', [])):
            raise ValueError('attachment_index_invalid')
        try:
            if platform not in self.instances:
                module, name = entry['factory'].split(':')
                factory = getattr(importlib.import_module(module), name)
                self.instances[platform] = factory(entry['config'], str(self.cache))
            result = self.instances[platform].prepare_attachment(event, index)
        except (ImportError, AttributeError, TypeError) as exc:
            raise ValueError('attachment_provider_unavailable') from exc
        if not isinstance(result, dict):
            raise ValueError('attachment_provider_invalid_result')
        original = event['message']['attachments'][index]
        for key in ('name', 'md5', 'size_bytes'):
            if original.get(key) is not None and result.get(key) != original[key]:
                raise ValueError('attachment_provider_identity_mismatch')
        if result.get('archive_path') and not isinstance(result.get('archive_path'), str):
            raise ValueError('attachment_provider_invalid_result')
        return result
