"""Refresh source snapshots independently of selection and its checkpoint."""
from contextlib import contextmanager
import importlib
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


@contextmanager
def source_snapshot(config):
    """Block refresh while reading; fail closed if another writer changes any DB."""
    with InstanceLock(_lock_path(config), timeout=45):
        before = _fingerprint(config)
        yield _metadata(config, before)
        if before != _fingerprint(config):
            raise RuntimeError('Source snapshot changed during query; retry the same window')


def collect_once(config):
    """Publish collection metadata only after the native refresh has succeeded."""
    with InstanceLock(_lock_path(config), timeout=45):
        adapter = importlib.import_module('collector.adapters.' + config['platform'])
        refresh = (adapter.refresh(config['source']) if config['source'].get('refresh', True)
                   else {'mode': 'decrypted_only'})
        result = dict(_metadata(config, _fingerprint(config)), refresh=refresh,
                      collected_at=int(time.time()))
        path = Path(config['storage']['state_file'] + '.collection.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.json.tmp')
        try:
            with temporary.open('w', encoding='utf-8') as stream:
                stream.write(canonical(result))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return result
