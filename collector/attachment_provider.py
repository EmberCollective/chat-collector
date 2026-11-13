"""Resolve a registered message attachment through existing local collectors."""
from __future__ import annotations

import importlib
import hashlib
import json
import tempfile
from pathlib import Path

from collector.assets import AttachmentArchive
from collector.config import load_config
from collector.__main__ import InstanceLock
from collector.store import digest


class CollectorAttachmentProvider:
    def __init__(self, config_path, cache_dir):
        self.path = config_path
        self.archives = {}
        self.cache = Path(cache_dir)/'resolved'

    def prepare_attachment(self, event, index):
        message = event['message']
        platform = message.get('platform')
        config = load_config(self.path)
        if config['platform'] != platform or str(config['source']['account_id']) != message.get('account_id'):
            raise ValueError('attachment_account_mismatch')
        if not config.get('attachments', {}).get('enabled', False):
            raise ValueError('attachment_archive_disabled')
        # Explicit model reads can decode on demand even when scheduled scans store metadata.
        config.setdefault('processing', {})['intensity'] = 'full'
        original = message['attachments'][index]
        cache_key = digest([platform,message['account_id'],event['message_key'],index,
            {k:original.get(k) for k in ('type','name','md5','size_bytes','local_path')}])
        cache = self.cache/(cache_key+'.json')
        if cache.is_file() and cache.stat().st_size < 65536:
            try:
                saved = json.loads(cache.read_text(encoding='utf-8'))
                path = Path(saved['archive_path']).resolve()
                if (all(original.get(k) is None or original[k] == saved.get(k) for k in ('type','name','md5','size_bytes'))
                        and path.is_relative_to(Path(config['attachments']['archive_dir']).resolve()) and path.is_file()):
                    with path.open('rb') as stream:
                        if hashlib.file_digest(stream,'sha256').hexdigest() == saved['sha256']:
                            return saved
            except (OSError,ValueError,KeyError,TypeError):
                pass
        if platform not in self.archives:
            self.archives[platform] = AttachmentArchive(config, platform, message['account_id'])
        archive = self.archives[platform]
        with InstanceLock(config['storage']['state_file'], timeout=45):
            # Prefer the fresh row's attachment paths, but only for the same message and asset.
            refreshed = original
            adapter = importlib.import_module('collector.adapters.' + platform)
            for row in adapter.scan(config['source'], message['sent_at'], message['sent_at'] + 1,
                    conversations={(message.get('conversation_type', 'direct'), message['conversation_id'])}):
                if row['message_id'] != message['message_id']:
                    continue
                choices = row.get('attachments', [])
                if index < len(choices):
                    candidate = choices[index]
                    if all(not original.get(key) or original.get(key) == candidate.get(key)
                           for key in ('type', 'name', 'md5', 'size_bytes')):
                        refreshed = candidate
                break
            result = archive.enrich(dict(message, attachments=[refreshed]))['attachments'][0]
        if result.get('archive_path') and result.get('sha256'):
            self.cache.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=self.cache,delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(result,stream,ensure_ascii=False)
                temporary.replace(cache)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return result
