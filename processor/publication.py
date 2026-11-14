"""Read-only compatibility API for consumers of immutable exported task batches."""
import hashlib
import json
from pathlib import Path


def read_batches(directory, known):
    events, files = [], []
    for path in sorted(Path(directory).glob('*.jsonl')):
        before = path.stat()
        name = str(path.resolve())
        previous = known.get(name)
        if previous and (previous['size'], previous['mtime']) == (before.st_size, before.st_mtime_ns):
            continue
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError('Task batch changed during import')
        sha = hashlib.sha256(raw).hexdigest()
        if previous and previous['sha256'] != sha:
            raise ValueError('An imported immutable task batch was modified')
        if not previous:
            for line in raw.decode('utf-8').split('\n'):
                if not line.strip():
                    continue
                if len(line) > 16 * 1024 * 1024:
                    raise ValueError('Task event exceeds 16 MiB')
                events.append(json.loads(line))
        files.append((name, after.st_size, after.st_mtime_ns, sha))
    return events, files
