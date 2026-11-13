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
    found = merge_candidates(candidates(before, dirty), candidates(current, dirty))
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
    assert candidates(path, [2]) == {'keyed': None}


def test_unchanged_pages_have_no_row_work(snapshots):
    before, current = snapshots
    assert candidates(current, changed_pages(before, current)) == {}
