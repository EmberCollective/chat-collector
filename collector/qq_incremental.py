"""Publish QQ's nonstandard page layout as ordinary SQLite using logical row deltas."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from .native_sqlite import NativeSQLite


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def _stamp(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_ino]


def _verify(db, *, full=True):
    # Native INSERT/DELETE maintains indexes; later rounds still check every page structurally.
    if db.execute('PRAGMA ' + ('integrity_check' if full else 'quick_check')).fetchall() != [('ok',)]:
        raise RuntimeError('qq_logical_snapshot_integrity_failed')


def _fault(stage):
    """Failure injection for publication/transaction regression tests."""


def _layout(source, name, sql):
    info = source.execute('PRAGMA table_xinfo(' + _quote(name) + ')')
    columns = [row[1] for row in info if row[6] == 0]
    rowid = next((alias for alias in ('rowid', '_rowid_', 'oid')
                  if alias not in {row[1].lower() for row in info}), None)
    if re.search(r'\bWITHOUT\s+ROWID\b', sql, re.I):
        rowid = None
    fields = ([rowid] if rowid else []) + columns
    return rowid, fields


def _packed_query(name, fields, *, order=None, identity=False, restricted=False):
    # Compare one native JSON value per row; only changed values cross into Python types.
    values = [f"CASE typeof({field}) WHEN 'blob' THEN json_object('b',hex({field})) "
              f"WHEN 'real' THEN json_object('f',printf('%!.17g',{field})) ELSE {field} END"
              for field in map(_quote, fields)]
    prefix = _quote(order) + ',' if identity else ''
    sql = f'SELECT {prefix}json_array({",".join(values)}) FROM {_quote(name)} NOT INDEXED'
    if restricted:
        sql += f' WHERE {_quote(order)} IN (SELECT value FROM json_each(?))'
    if order:
        sql += ' ORDER BY ' + _quote(order)
    return sql


def _unpack(packed):
    return tuple((bytes.fromhex(value['b']) if 'b' in value else float(value['f']))
                 if isinstance(value, dict) else value for value in json.loads(packed))


def _records(source, name, fields, *, order=None):
    for packed, in source.iterate(_packed_query(name, fields, order=order)):
        yield _unpack(packed)


def _copy_sequences(source, destination, schema):
    if any(name == 'sqlite_sequence' for _, name, _ in schema):
        expected = source.execute('SELECT name,seq FROM sqlite_sequence ORDER BY name')
        if destination.execute('SELECT name,seq FROM sqlite_sequence ORDER BY name').fetchall() != expected:
            destination.execute('DELETE FROM sqlite_sequence')
            destination.executemany('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', expected)


def _export(source, destination, schema):
    rows_written = 0
    destination.execute('PRAGMA encoding=' + source.execute('PRAGMA encoding')[0][0].replace('-', ''))
    destination.execute('BEGIN IMMEDIATE')
    for kind, name, sql in schema:
        if kind != 'table' or name.startswith('sqlite_'):
            continue
        destination.execute(sql)
        _, fields = _layout(source, name, sql)
        columns, width = ','.join(map(_quote, fields)), len(fields)
        before = destination.total_changes
        destination.executemany(f'INSERT INTO {_quote(name)} ({columns}) VALUES({",".join("?" for _ in range(width))})',
                                _records(source, name, fields))
        rows_written += destination.total_changes - before
    _copy_sequences(source, destination, schema)
    for kind, name, sql in schema:
        if kind in {'index', 'view', 'trigger'} and not name.startswith('sqlite_'):
            destination.execute(sql)
    return rows_written


def _merge_table(source, destination, name, sql, rowids=None):
    rowid, fields = _layout(source, name, sql)
    if rowid is None:
        raise ValueError('QQ incremental tables require an accessible rowid')
    table, identity = _quote(name), _quote(rowid)
    columns, width = ','.join(map(_quote, fields)), len(fields)
    destination.execute(f'CREATE TEMP TABLE qq_delta AS SELECT {columns} FROM main.{table} WHERE 0')
    destination.execute('CREATE TEMP TABLE qq_deleted(id INTEGER PRIMARY KEY)')
    insert_delta = f'INSERT INTO temp.qq_delta VALUES({",".join("?" for _ in range(width))})'
    query = _packed_query(name, fields, order=rowid, identity=True, restricted=rowids is not None)
    parameters = (json.dumps(sorted(rowids)),) if rowids is not None else ()
    source_rows = source.iterate(query, parameters)
    target_rows = destination.execute(query, parameters)
    inserted = deleted = 0
    try:
        new, old = next(source_rows, None), next(target_rows, None)
        while new is not None or old is not None:
            if old is None or (new is not None and new[0] < old[0]):
                destination.execute(insert_delta, _unpack(new[1]))
                inserted += 1
                new = next(source_rows, None)
            elif new is None or old[0] < new[0]:
                destination.execute('INSERT INTO temp.qq_deleted VALUES(?)', (old[0],))
                deleted += 1
                old = next(target_rows, None)
            else:
                if new != old:
                    destination.execute('INSERT INTO temp.qq_deleted VALUES(?)', (old[0],))
                    destination.execute(insert_delta, _unpack(new[1]))
                    inserted += 1
                    deleted += 1
                new, old = next(source_rows, None), next(target_rows, None)
    finally:
        source_rows.close()
        target_rows.close()
    # Remove all old versions first so valid swaps of UNIQUE values do not conflict.
    destination.execute(f'DELETE FROM main.{table} WHERE {identity} IN (SELECT id FROM temp.qq_deleted)')
    destination.execute(f'INSERT INTO main.{table} ({columns}) SELECT * FROM temp.qq_delta')
    destination.execute('DROP TABLE temp.qq_delta')
    destination.execute('DROP TABLE temp.qq_deleted')
    return inserted, deleted


def sync_plaintext(raw, target, library, *, candidates=None, previous_raw_stamp=None):
    """The caller owns the collector lock; source pages were verified before this call."""
    started = time.perf_counter()
    raw, target = Path(raw).resolve(strict=True), Path(target).resolve()
    if raw == target:
        raise ValueError('QQ public snapshot must be separate from the private page cache')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = target.parent / '.incremental' / ('logical-' + target.name)
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging_prefix, state_prefix = 'baseline-', 'state-'
    # The caller's collector lock makes these target-specific crash leftovers safe to remove.
    for leftover in staging_dir.iterdir():
        if leftover.name.startswith((staging_prefix, state_prefix)) and leftover.is_file():
            leftover.unlink()
    state = target.with_suffix('.logical.json')
    try:
        previous = json.loads(state.read_text(encoding='utf-8')) if state.exists() else {}
    except (ValueError, UnicodeError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    target_valid = target.exists()
    if target_valid:
        # Opening for a real read also recovers any interrupted rollback journal.
        try:
            with closing(sqlite3.connect(target)) as destination:
                destination.execute('SELECT count(*) FROM sqlite_master').fetchone()
        except sqlite3.DatabaseError:
            target_valid = False
    raw_stamp = _stamp(raw)
    if previous.get('raw') == [str(raw), raw_stamp] and target_valid and previous.get('target') == _stamp(target):
        return {'changed': False, 'mode': 'logical_incremental', 'rows_written': 0, 'rows_deleted': 0,
                'candidate_rows': 0,
                'timings_seconds': {'logical_sync': round(time.perf_counter() - started, 3)}}
    temporary = None
    with NativeSQLite(raw, readonly=True, library=library, pending_byte=0x3ffffc00) as source:
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        schema = source.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")
        if any(kind == 'table' and sql.lstrip().upper().startswith('CREATE VIRTUAL TABLE') for kind, _, sql in schema):
            raise ValueError('QQ logical refresh does not support virtual tables')
        digest = hashlib.sha256(json.dumps(schema, ensure_ascii=False).encode()).hexdigest()
        full = (not target_valid or previous.get('schema') != digest
                or previous.get('target') != _stamp(target)
                or any(kind == 'table' and not name.startswith('sqlite_') and _layout(source, name, sql)[0] is None
                       for kind, name, sql in schema))
        limited = (candidates is not None and not full and previous_raw_stamp is not None
                   and previous.get('raw') == [str(raw), list(previous_raw_stamp)])
        candidate_rows = sum(len(set(rowids)) for rowids in candidates.values() if rowids is not None) if limited else None
        rows_written = rows_deleted = 0
        if full:
            with tempfile.NamedTemporaryFile(dir=staging_dir, prefix=staging_prefix, suffix='.db', delete=False) as stream:
                temporary = Path(stream.name)
        try:
            with closing(sqlite3.connect(temporary if full else target, isolation_level=None)) as destination:
                destination.execute('PRAGMA journal_mode=DELETE')
                destination.execute('PRAGMA synchronous=FULL')
                if full:
                    rows_written = _export(source, destination, schema)
                else:
                    destination.execute('BEGIN IMMEDIATE')
                    # Replication must not replay application triggers against copied tables.
                    for kind, name, _ in schema:
                        if kind == 'trigger':
                            destination.execute('DROP TRIGGER ' + _quote(name))
                    for kind, name, sql in schema:
                        if kind == 'table' and not name.startswith('sqlite_'):
                            rowids = candidates.get(name, []) if limited else None
                            if rowids is not None and not rowids:
                                continue
                            inserted, deleted = _merge_table(source, destination, name, sql, rowids)
                            rows_written += inserted
                            rows_deleted += deleted
                    _copy_sequences(source, destination, schema)
                    for kind, _, sql in schema:
                        if kind == 'trigger':
                            destination.execute(sql)
                for pragma in ('user_version', 'application_id'):
                    expected = int(source.execute('PRAGMA ' + pragma)[0][0])
                    if destination.execute('PRAGMA ' + pragma).fetchone()[0] != expected:
                        destination.execute(f'PRAGMA {pragma}={expected}')
                # Only the standard public DB may carry collector-owned query indexes.
                for table in ('group_msg_table', 'c2c_msg_table'):
                    if '40050' in {row[1] for row in destination.execute(f'PRAGMA table_info("{table}")')}:
                        destination.execute(f'CREATE INDEX IF NOT EXISTS "_collector_time_{table}" ON "{table}"("40050")')
                _fault('before_integrity_check')
                _verify(destination, full=full)
                if _stamp(raw) != raw_stamp:
                    raise RuntimeError('qq_private_snapshot_changed_during_sync')
                _fault('before_commit')
                destination.execute('COMMIT')
            if full:
                _fault('before_publish')
                if any(Path(str(target) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
                    raise RuntimeError('qq_public_snapshot_has_active_sidecar')
                temporary.replace(target)
            record = {'raw': [str(raw), raw_stamp], 'target': _stamp(target), 'schema': digest}
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=staging_dir, prefix=state_prefix, delete=False) as stream:
                state_temp = Path(stream.name)
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                state_temp.replace(state)
            finally:
                state_temp.unlink(missing_ok=True)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return {'changed': True, 'mode': 'logical_baseline' if full else 'logical_incremental',
            'rows_written': rows_written, 'rows_deleted': rows_deleted, 'quick_check_ok': True,
            'candidate_rows': candidate_rows, 'validation': 'integrity_check' if full else 'quick_check',
            'timings_seconds': {'logical_sync': round(time.perf_counter() - started, 3)}}
