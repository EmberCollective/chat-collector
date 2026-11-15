from contextlib import closing
from pathlib import Path
import shutil
import sqlite3

import pytest

from collector.native_delta import capture_rows, merge_candidates
from collector.native_sqlite import NativeSQLite

NATIVE = Path(__file__).resolve().parents[1] / 'runtime-deps/native'
pytestmark = pytest.mark.skipif(not (NATIVE/'sqlite-dbdata-qq.dll').exists(), reason='Build official dbdata extension first')


def candidates(path, dirty):
    with NativeSQLite(path, readonly=True, library=NATIVE/'sqlcipher-qq-pages.dll', pending_byte=0x3ffffc00) as db:
        return capture_rows(db, dirty)


def merged(before, current, dirty):
    old, new = candidates(before, dirty), candidates(current, dirty)
    with NativeSQLite(current, readonly=True, library=NATIVE/'sqlcipher-qq-pages.dll', pending_byte=0x3ffffc00) as db:
        from collector.native_delta import _register
        _register(db)
        return merge_candidates(old, new, db)


def changed_pages(before, after):
    a, b = before.read_bytes(), after.read_bytes()
    return {i//4096+1 for i in range(0, max(len(a), len(b)), 4096)
            if a[i:i+4096] != b[i:i+4096]}


def data(path):
    with closing(sqlite3.connect(path)) as db:
        return dict(db.execute('SELECT id,body FROM messages'))


@pytest.fixture
def snapshots(tmp_path):
    current = tmp_path/'current.db'
    before = tmp_path/'before.db'
    with closing(sqlite3.connect(current)) as db, db:
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, body BLOB)')
        db.execute('CREATE INDEX message_id ON messages(id)')
        db.executemany('INSERT INTO messages VALUES(?,?)', [(i, b'x'*6000) for i in range(1,120)])
        db.execute('INSERT INTO messages VALUES(-9223372036854775807,?)', (b'negative',))
    shutil.copyfile(current, before)
    return before, current


@pytest.mark.parametrize('operation', ['edit_delete_append', 'overflow_only', 'shrink', 'delete_all'])
def test_candidates_include_every_changed_row(snapshots, operation):
    before, current = snapshots
    with closing(sqlite3.connect(current)) as db, db:
        if operation == 'edit_delete_append':
            db.execute('UPDATE messages SET body=? WHERE id IN (1,50,-9223372036854775807)', (b'changed',))
            db.execute('DELETE FROM messages WHERE id BETWEEN 80 AND 99')
            db.executemany('INSERT INTO messages VALUES(?,?)', [(i,b'new'*2000) for i in range(900,950)])
        elif operation == 'overflow_only':
            with db.blobopen('messages', 'body', 1) as blob:
                blob.seek(5700)
                blob.write(b'only-overflow')
        elif operation == 'shrink':
            db.execute('DELETE FROM messages WHERE id>10')
            db.commit()
            db.execute('VACUUM')
        else:
            db.execute('DELETE FROM messages')
    dirty = changed_pages(before, current)
    found = merged(before, current, dirty)
    old, new = data(before), data(current)
    actual = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    assert actual <= set(found['messages'])
    if operation == 'overflow_only':
        assert 1 in found['messages'] and len(found['messages']) < 5


def test_without_rowid_requests_table_fallback(tmp_path):
    path = tmp_path/'source.db'
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE keyed(name TEXT PRIMARY KEY,value TEXT) WITHOUT ROWID')
        db.execute("INSERT INTO keyed VALUES('name','value')")
    assert candidates(path, [2]).rows == {'keyed': None}


def test_unchanged_pages_have_no_row_work(snapshots):
    before, current = snapshots
    assert merged(before, current, changed_pages(before, current)) == {}


def test_interior_change_does_not_expand_to_unchanged_subtree(tmp_path):
    before, current = tmp_path/'before.db', tmp_path/'current.db'
    with closing(sqlite3.connect(current)) as db, db:
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,body BLOB)')
        db.executemany('INSERT INTO messages VALUES(?,?)', ((i, b'x'*500) for i in range(10000)))
    shutil.copyfile(current, before)
    with closing(sqlite3.connect(current)) as db, db:
        db.execute('INSERT INTO messages VALUES(10000,?)', (b'new',))
        root = db.execute("SELECT rootpage FROM sqlite_schema WHERE name='messages'").fetchone()[0]
    dirty = changed_pages(before, current) | {root}
    found = merged(before, current, dirty)['messages']
    assert 10000 in found and len(found) < 100


def test_detached_unchanged_leaves_still_include_all_deleted_rows(snapshots):
    before, current = snapshots
    with NativeSQLite(current, readonly=True, library=NATIVE/'sqlcipher-qq-pages.dll', pending_byte=0x3ffffc00) as native:
        leaves = {row[0] for row in native.execute("SELECT pageno FROM dbstat WHERE name='messages' AND pagetype='leaf'")}
    with closing(sqlite3.connect(current)) as db, db:
        db.execute('DELETE FROM messages')
    dirty = changed_pages(before, current)
    assert leaves - dirty  # SQLite can free leaves without rewriting their row payload.
    assert set(data(before)) <= set(merged(before, current, dirty)['messages'])


def test_reused_pages_and_row_moves_preserve_table_identity(tmp_path):
    before, current = tmp_path/'before.db', tmp_path/'current.db'
    with closing(sqlite3.connect(current)) as db, db:
        for name in ('messages', 'other'):
            db.execute(f'CREATE TABLE {name}(id INTEGER PRIMARY KEY,body BLOB)')
        db.executemany('INSERT INTO messages VALUES(?,?)', ((i, b'x'*5000) for i in range(1,400)))
    shutil.copyfile(current, before)
    with closing(sqlite3.connect(current)) as db, db:
        db.execute('DELETE FROM messages')
        db.executemany('INSERT INTO other VALUES(?,?)', ((i, b'y'*5000) for i in range(200,500)))
    found = merged(before, current, changed_pages(before, current))
    assert set(range(1,400)) <= set(found['messages'])
    assert set(range(200,500)) <= set(found['other'])


def test_unchanged_leaf_reassignment_does_not_lose_rows(tmp_path):
    before, current = tmp_path/'before.db', tmp_path/'current.db'
    with closing(sqlite3.connect(current)) as db, db:
        db.executescript('''
            CREATE TABLE messages(id INTEGER PRIMARY KEY,body BLOB);
            CREATE TABLE other(id INTEGER PRIMARY KEY,body BLOB);
            INSERT INTO messages VALUES(-9223372036854775808,x'01');
            INSERT INTO other VALUES(9223372036854775807,x'02');
        ''')
        roots = dict(db.execute("SELECT name,rootpage FROM sqlite_schema WHERE type='table'"))
    shutil.copyfile(current, before)
    with closing(sqlite3.connect(current)) as db, db:
        db.execute('PRAGMA writable_schema=ON')
        db.execute("UPDATE sqlite_schema SET rootpage=CASE name WHEN 'messages' THEN ? ELSE ? END WHERE type='table'",
                   (roots['other'],roots['messages']))
        version = db.execute('PRAGMA schema_version').fetchone()[0]
        db.execute(f'PRAGMA schema_version={version+1}')
    dirty = changed_pages(before, current)
    assert not set(roots.values()) & dirty
    found = merged(before, current, dirty)
    assert set(found['messages']) == set(found['other']) == {-(2**63),2**63-1}


def test_page_walk_and_rowid_queries_do_not_scan_or_sort_unrelated_rows(snapshots):
    _, current = snapshots
    with NativeSQLite(current, readonly=True, library=NATIVE/'sqlcipher-qq-pages.dll', pending_byte=0x3ffffc00) as db:
        capture_rows(db, [2])
        walk = db.execute('EXPLAIN QUERY PLAN SELECT path,pageno,pagetype FROM dbstat WHERE name=? ORDER BY name,path', ('messages',))
        assert 'TEMP B-TREE' not in str(walk)
        rows = db.execute('''EXPLAIN QUERY PLAN SELECT data.value FROM json_each(?) AS page
            JOIN sqlite_dbdata AS data ON data.pgno=page.value WHERE data.field=-1''', ('[2]',))
        assert 'data VIRTUAL TABLE INDEX 2:' in str(rows)
