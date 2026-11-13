"""Runnable provider example for an explicitly mapped local archive, without any AI."""
import hashlib
from pathlib import Path
import tomllib


class LocalFileProvider:
    def __init__(self, config_path, cache_dir):
        self.path = Path(config_path).resolve()
        with self.path.open('rb') as stream:
            self.config = tomllib.load(stream)

    def prepare_attachment(self, event, index):
        message = event['message']
        if (message['platform'], message['account_id']) != (self.config['platform'], self.config['account_id']):
            raise ValueError('attachment_account_mismatch')
        attachment = message['attachments'][index]
        ref = attachment.get('provider_ref')
        if not isinstance(ref, str) or ref not in self.config['files']:
            raise ValueError('unknown_provider_attachment')
        # Only the owner's local configuration maps opaque IDs to files.
        path = (self.path.parent / self.config['files'][ref]).resolve(strict=True)
        with path.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        return dict(attachment, archive_path=str(path), sha256=sha, status='archived')
