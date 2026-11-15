"""Bounded, cached logical-size snapshots of explicitly configured output paths."""
from copy import deepcopy
import os
from pathlib import Path
import stat
import threading
import time


def _stat(path):
    # lstat every ancestor: a configured junction must not turn into a disk scan.
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('link_or_reparse_point')
    return info


class StorageMetrics:
    def __init__(self, *, ttl=60, max_entries=20000, max_seconds=.5):
        self.ttl, self.max_entries, self.max_seconds = ttl, max_entries, max_seconds
        self.lock = threading.Lock()
        self.cached_key = self.cached_value = None
        self.expires = 0

    def measure(self, specs, sources):
        key = (tuple((s['path'], tuple(s['source_ids']), s['category'], s.get('optional', False), s.get('declared', True)) for s in specs),
               tuple((s['id'], s['name']) for s in sources))
        with self.lock:
            if key == self.cached_key and time.monotonic() < self.expires:
                result = deepcopy(self.cached_value)
                result['cached'] = True
                return result
            result = self._scan(specs, sources)
            self.cached_key, self.cached_value = key, result
            self.expires = time.monotonic() + self.ttl
            return deepcopy(result)

    def _scan(self, specs, sources):
        measured = time.time()
        deadline = time.monotonic() + self.max_seconds
        scope, errors, files, visited = [], [], {}, set()
        entries, truncated = 0, False
        for spec in specs:
            item = dict(spec, path=os.path.abspath(spec['path']), status='ok', directory=False)
            path = Path(item['path'])
            try:
                if not item.get('declared', True):
                    # A loader's resolved default no longer preserves junction identity.
                    item['status'] = 'not_provided'
                    scope.append(item)
                    continue
                if time.monotonic() >= deadline:
                    truncated = True
                    item['status'] = 'budget_exceeded'
                    scope.append(item)
                    continue
                if path == Path(path.anchor):
                    raise ValueError('filesystem_root')
                info = _stat(path)
                item['directory'] = stat.S_ISDIR(info.st_mode)
                if not item['directory'] and not stat.S_ISREG(info.st_mode):
                    raise ValueError('not_regular_file')
            except FileNotFoundError:
                item['status'] = 'absent_optional' if item.get('optional') else 'missing'
            except OSError:
                item['status'] = 'unreadable'
            except ValueError:
                item['status'] = 'blocked'
            scope.append(item)

        def owners(path):
            result = set()
            for item in scope:
                base = Path(item['path'])
                if path == base or item['directory'] and base in path.parents:
                    result.update(item['source_ids'] or [None])
            return result

        def visit(path, depth=0):
            nonlocal entries, truncated
            if entries >= self.max_entries or time.monotonic() >= deadline or depth > 32:
                truncated = True
                errors.append((path, 'budget_exceeded'))
                return
            entries += 1
            try:
                info = _stat(path)
                if stat.S_ISREG(info.st_mode):
                    if not info.st_ino:
                        errors.append((path, 'file_identity_unavailable'))
                        return
                    identity = (info.st_dev, info.st_ino)
                    previous = files.setdefault(identity, {'bytes': info.st_size, 'owners': set()})
                    previous['owners'].update(owners(path))
                    if previous['bytes'] != info.st_size:
                        errors.append((path, 'changed_during_scan'))
                elif stat.S_ISDIR(info.st_mode):
                    if path in visited:
                        return
                    visited.add(path)
                    with os.scandir(path) as stream:
                        for entry in stream:
                            visit(Path(entry.path), depth + 1)
                            if truncated:
                                break
                else:
                    errors.append((path, 'not_regular_file'))
            except ValueError:
                errors.append((path, 'link_or_reparse_point'))
            except OSError:
                errors.append((path, 'unreadable'))

        for item in sorted(scope, key=lambda s: len(Path(s['path']).parts)):
            if item['status'] == 'ok':
                visit(Path(item['path']))
        for item in scope:
            base = Path(item['path'])
            if item['status'] == 'ok' and any(path == base or base in path.parents for path, _ in errors):
                item['status'] = 'partial'
        amounts = {source['id']: 0 for source in sources}
        amounts[None] = 0
        for value in files.values():
            owner = next(iter(value['owners'])) if len(value['owners']) == 1 else None
            amounts[owner if owner in amounts else None] += value['bytes']

        def row(identifier):
            relevant = scope if identifier is None else [item for item in scope if identifier in item['source_ids']]
            complete = bool(relevant) and all(item['status'] in {'ok', 'absent_optional'} for item in relevant)
            return {'logical_bytes': amounts[identifier] if complete else None,
                    'known_logical_bytes': amounts[identifier],
                    'status': 'ok' if complete else 'partial' if relevant else 'not_provided'}

        rows = [dict(source_id=source['id'], name=source['name'], **row(source['id'])) for source in sources]
        shared = row(None)
        complete = all(item['status'] in {'ok', 'absent_optional'} for item in scope) and bool(scope)
        complete = complete and all(item['status'] == 'ok' for item in rows)
        known = sum(value['bytes'] for value in files.values())
        return {'basis': 'logical_bytes', 'physical_bytes': None, 'measured_at': measured,
                'measurement_finished_at': time.time(), 'cached': False, 'cache_ttl_seconds': self.ttl,
                'complete': complete, 'truncated': truncated, 'total_logical_bytes': known if complete else None,
                'known_logical_bytes': known, 'entries_examined': entries, 'files_counted': len(files),
                'scope': scope, 'sources': rows, 'shared': shared,
                'warnings': sorted({code for _, code in errors})}
