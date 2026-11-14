"""Refresh source snapshots independently of selection and its checkpoint."""
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import time

from common.codec import canonical, digest
from common.locking import InstanceLock


def _paths(config):
    source = config['source']
    folder = Path(source.get('decrypted_dir', '.')).resolve()
    paths = set(folder.glob('*.db')) if source.get('decrypted_dir') else set()
    if source.get('database'):
        paths.add(Path(source['database']).resolve())
    if source.get('resource_db'):
        resource = Path(source['resource_db']).resolve()
        paths.update((resource, resource.parent / 'hardlink.db'))
    return sorted(paths)


def _fingerprint(config):
    result = []
    for path in _paths(config):
        for suffix in ('', '-wal', '-journal'):
            part = Path(str(path) + suffix)
            try:
                stamp = part.stat()
                result.append([str(part), stamp.st_size, stamp.st_mtime_ns, stamp.st_ino])
            except FileNotFoundError:
                result.append([str(part), None])
    return result


def _lock_path(config):
    source = config['source']
    if source.get('decrypted_dir'):
        return Path(source['decrypted_dir']) / '.collection'
    return source.get('database') or config['storage']['state_file'] + '.collection'


def _metadata(config, fingerprint):
    identity = [config['platform'], str(config['source']['account_id'])]
    return dict(platform=identity[0], provider_id=identity[0], account_id=identity[1],
                snapshot_id=digest([identity, fingerprint]), files=fingerprint)


def publication_path(config):
    """Collection progress belongs to the source, independently of selection state."""
    source = config['source']
    if source.get('decrypted_dir'):
        return Path(source['decrypted_dir']) / '.collection.json'
    return Path(str(source.get('database') or config['storage']['state_file']) + '.collection.json')


def _published(config):
    path = publication_path(config)
    if not path.is_file() and config.get('storage'):
        path = Path(config['storage']['state_file'] + '.collection.json')
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None


def _pending_path(config):
    return publication_path(config).with_suffix('.pending.json')


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def source_snapshot(config):
    """Block refresh while reading; fail closed if another writer changes any DB."""
    with InstanceLock(_lock_path(config), timeout=45):
        before = _fingerprint(config)
        metadata = _metadata(config, before)
        metadata['capture_managed'] = config['source'].get('refresh', True)
        saved = _published(config)
        if ((saved is not None and saved.get('snapshot_id') != metadata['snapshot_id']) or
                (_pending_path(config).exists() and saved is None)):
            raise RuntimeError('Source snapshot changed without complete collection publication; retry collection')
        if saved is not None:
            metadata['capture_managed'] = True
            if saved.get('snapshot_id') == metadata['snapshot_id']:
                metadata['capture_started_at'] = saved.get('capture_started_at')
        yield metadata
        if before != _fingerprint(config):
            raise RuntimeError('Source snapshot changed during query; retry the same window')


def collect_once(config, *, force=True):
    """Publish collection metadata only after the native refresh has succeeded."""
    with InstanceLock(_lock_path(config), timeout=45):
        capture_started_at = int(time.time())
        if not force and config['source'].get('refresh', True):
            saved = _published(config)
            interval = config.get('schedule', {}).get('refresh_interval_seconds', 0)
            if (saved is not None and not _pending_path(config).exists()
                    and saved.get('snapshot_id') == _metadata(config, _fingerprint(config))['snapshot_id']
                    and type(saved.get('collected_at')) is int
                    and type(saved.get('capture_started_at')) is int):
                remaining = interval - (capture_started_at - saved['collected_at'])
                if remaining > 0:
                    return dict(saved, status='waiting_for_refresh', retry_after_seconds=remaining)
        adapter = importlib.import_module('collector.adapters.' + config['platform'])
        pending = _pending_path(config)
        if not config['source'].get('refresh', True) and pending.exists():
            raise RuntimeError('Source collection is incomplete; enable refresh to recover it')
        # A crash between per-database commits must never expose a mixed source generation.
        _write_json(pending, dict(capture_started_at=capture_started_at))
        refresh = (adapter.refresh(config['source']) if config['source'].get('refresh', True)
                   else {'mode': 'decrypted_only'})
        result = dict(_metadata(config, _fingerprint(config)), refresh=refresh,
                      collected_at=int(time.time()), capture_started_at=capture_started_at)
        _write_json(publication_path(config), result)
        pending.unlink()
        return result
