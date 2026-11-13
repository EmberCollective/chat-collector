from contextlib import closing
from pathlib import Path
import shutil
import sqlite3

import pytest

from collector import qq_incremental


def test_public_time_index_does_not_change_native_page_layout(fixture):
    raw, target, library = fixture
    with closing(sqlite3.connect(raw)) as db, db:
        db.execute('CREATE TABLE group_msg_table("40001" INTEGER PRIMARY KEY,"40050" INTEGER)')
        db.execute('INSERT INTO group_msg_table VALUES(1,100)')
    before = raw.read_bytes()
    qq_incremental.sync_plaintext(raw, target, library)
    assert raw.read_bytes() == before
    with closing(sqlite3.connect(target)) as db:
        plan = db.execute('EXPLAIN QUERY PLAN SELECT * FROM group_msg_table WHERE "40050">=100').fetchall()
        assert '_collector_time_group_msg_table' in str(plan)


@pytest.fixture
def fixture(tmp_path):
    original_library = Path(__file__).parents[1] / 'runtime-deps/native/sqlcipher-pages.dll'
    if not original_library.exists():
        pytest.skip('Build the official SQLCipher page interface first')
    library = tmp_path / 'sqlcipher-qq-pages.dll'
    shutil.copyfile(original_library, library)
    raw = tmp_path / 'private.db'
    target = tmp_path / 'public.db'
    with closing(sqlite3.connect(raw)) as db, db:
        db.executescript('''
            CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT UNIQUE, attachment BLOB);
            CREATE INDEX message_body ON messages(body);
            CREATE TABLE audit(value TEXT);
            CREATE TRIGGER message_change AFTER UPDATE ON messages BEGIN INSERT INTO audit VALUES('changed'); END;
            CREATE VIEW visible AS SELECT id,body FROM messages;
            INSERT INTO messages VALUES(1,'one',x'0102'),(8,'eight',NULL),(20,'twenty',x'00');
            DELETE FROM messages WHERE id=20;
            CREATE TABLE unkeyed(body TEXT);
            INSERT INTO unkeyed(rowid,body) VALUES(2,'two'),(99,'ninety-nine');
            PRAGMA user_version=42;
        ''')
    yield raw, target, library


def _rows(path, sql):
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        return db.execute(sql).fetchall()


def test_logical_updates_deletes_rowids_sequences_and_trigger_semantics(fixture):
    raw, target, library = fixture
    first = qq_incremental.sync_plaintext(raw, target, library)
    assert first['mode'] == 'logical_baseline'
    assert first['validation'] == 'integrity_check'
    assert _rows(target, 'SELECT seq FROM sqlite_sequence WHERE name="messages"') == [(20,)]
    assert _rows(target, 'SELECT rowid,* FROM unkeyed') == [(2, 'two'), (99, 'ninety-nine')]
    with closing(sqlite3.connect(raw)) as db, db:
        db.execute("UPDATE messages SET body='temporary' WHERE id=1")
        db.execute("UPDATE messages SET body='one' WHERE id=8")
        db.execute("UPDATE messages SET body='eight' WHERE id=1")
        db.execute("DELETE FROM unkeyed WHERE rowid=2")
        db.execute("INSERT INTO unkeyed(rowid,body) VALUES(5,'new')")
    second = qq_incremental.sync_plaintext(raw, target, library)
    assert second['mode'] == 'logical_incremental'
    assert second['validation'] == 'quick_check'
    assert second['rows_written'] == 6  # Two message versions, one new row, three source audit rows.
    for sql in ['SELECT * FROM messages ORDER BY id', 'SELECT * FROM audit', 'SELECT rowid,* FROM unkeyed',
                'SELECT * FROM sqlite_sequence', 'SELECT * FROM visible', 'PRAGMA user_version',
                'SELECT type,name,sql FROM sqlite_master ORDER BY name']:
        assert _rows(target, sql) == _rows(raw, sql)
    before = target.read_bytes()
    unchanged = qq_incremental.sync_plaintext(raw, target, library)
    assert not unchanged['changed'] and unchanged['rows_written'] == 0
    assert target.read_bytes() == before


@pytest.mark.parametrize('stage', ['before_integrity_check', 'before_commit'])
def test_logical_failure_rolls_back_public_rows_and_marker(fixture, monkeypatch, stage):
    raw, target, library = fixture
    qq_incremental.sync_plaintext(raw, target, library)
    before = target.read_bytes()
    marker = target.with_suffix('.logical.json').read_bytes()
    with closing(sqlite3.connect(raw)) as db, db:
        db.execute("UPDATE messages SET body='new version' WHERE id=1")
    def fail(where):
        if where == stage:
            raise OSError('injected disk failure')
    with monkeypatch.context() as patch:
        patch.setattr(qq_incremental, '_fault', fail)
        with pytest.raises(OSError, match='injected'):
            qq_incremental.sync_plaintext(raw, target, library)
    assert target.read_bytes() == before
    assert target.with_suffix('.logical.json').read_bytes() == marker
    assert qq_incremental.sync_plaintext(raw, target, library)['mode'] == 'logical_incremental'
    assert _rows(target, 'SELECT * FROM messages') == _rows(raw, 'SELECT * FROM messages')


def test_schema_change_stages_replacement_and_external_target_change_rebuilds(fixture, monkeypatch):
    raw, target, library = fixture
    qq_incremental.sync_plaintext(raw, target, library)
    before = target.read_bytes()
    with closing(sqlite3.connect(raw)) as db, db:
        db.execute('CREATE TABLE extra(id TEXT PRIMARY KEY,body TEXT) WITHOUT ROWID')
        db.execute("INSERT INTO extra VALUES('key','content')")
    def fail(stage):
        if stage == 'before_publish':
            raise OSError('injected publish failure')
    with monkeypatch.context() as patch:
        patch.setattr(qq_incremental, '_fault', fail)
        with pytest.raises(OSError, match='injected'):
            qq_incremental.sync_plaintext(raw, target, library)
    assert target.read_bytes() == before
    assert qq_incremental.sync_plaintext(raw, target, library)['mode'] == 'logical_baseline'
    assert _rows(target, 'SELECT * FROM extra') == [('key', 'content')]
    with closing(sqlite3.connect(target)) as db, db:
        db.execute("UPDATE messages SET body='outside change' WHERE id=1")
    assert qq_incremental.sync_plaintext(raw, target, library)['mode'] == 'logical_baseline'
    assert _rows(target, 'SELECT * FROM messages') == _rows(raw, 'SELECT * FROM messages')


def test_native_row_packing_preserves_sqlite_types_and_floating_precision(fixture):
    raw, target, library = fixture
    values = [None, b'', bytes(range(256)), '{"b":"not a blob"}', '中文\ntext',
              -(2**63), 2**63 - 1, 1.2345678901234567, 5e-324, 1.7976931348623157e308]
    with closing(sqlite3.connect(raw)) as db, db:
        db.execute('CREATE TABLE types(value)')
        db.executemany('INSERT INTO types VALUES(?)', ((value,) for value in values))
    qq_incremental.sync_plaintext(raw, target, library)
    assert _rows(target, 'SELECT typeof(value),value FROM types') == _rows(raw, 'SELECT typeof(value),value FROM types')


def test_page_candidates_only_apply_to_the_exact_last_published_raw_version(fixture, monkeypatch):
    raw, target, library = fixture
    qq_incremental.sync_plaintext(raw, target, library)
    previous_raw = qq_incremental._stamp(raw)
    with closing(sqlite3.connect(raw)) as db, db:
        db.execute("UPDATE messages SET body='new first' WHERE id=1")
        db.execute('DELETE FROM unkeyed WHERE rowid=99')
    visited = []
    original = qq_incremental._merge_table
    def record(*args):
        visited.append((args[2], args[4]))
        return original(*args)
    monkeypatch.setattr(qq_incremental, '_merge_table', record)
    result = qq_incremental.sync_plaintext(raw, target, library,
        candidates={'messages': [1], 'audit': None, 'unkeyed': [99]}, previous_raw_stamp=previous_raw)
    assert result['mode'] == 'logical_incremental'
    assert result['candidate_rows'] == 2
    assert visited == [('audit', None), ('messages', [1]), ('unkeyed', [99])]
    for table in ('messages', 'audit', 'unkeyed'):
        assert _rows(target, f'SELECT rowid,* FROM {table}') == _rows(raw, f'SELECT rowid,* FROM {table}')
    # Simulate a missed publication: candidates describe only the latest refresh.
    visited.clear()
    with closing(sqlite3.connect(raw)) as db, db:
        db.execute("UPDATE messages SET body='new eighth' WHERE id=8")
    qq_incremental.sync_plaintext(raw, target, library, candidates={}, previous_raw_stamp=previous_raw)
    assert all(rowids is None for _, rowids in visited)
    assert _rows(target, 'SELECT * FROM messages') == _rows(raw, 'SELECT * FROM messages')


def test_corrupt_public_header_is_rebuilt_from_verified_private_snapshot(fixture):
    raw, target, library = fixture
    qq_incremental.sync_plaintext(raw, target, library)
    with target.open('r+b') as stream:
        stream.write(b'broken public database header')
    assert qq_incremental.sync_plaintext(raw, target, library)['mode'] == 'logical_baseline'
    assert _rows(target, 'SELECT * FROM messages') == _rows(raw, 'SELECT * FROM messages')
