import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from collector import incremental
from collector.native_sqlite import NativeSQLite

sqlcipher=pytest.importorskip('sqlcipher3.dbapi2')
pytestmark=pytest.mark.skipif(not (Path(__file__).parents[1]/'runtime-deps/native/sqlcipher-pages.dll').exists(),
                            reason='Build the official SQLCipher page interface first')


@pytest.fixture(params=['wechat','qq'])
def fixture(request,tmp_path):
    platform=request.param
    folder=tmp_path/'source';folder.mkdir()
    original=folder/'source.db'
    key='a1'*32 if platform=='wechat' else 'test-only-key'
    db=sqlcipher.connect(str(original),isolation_level=None)
    db.execute('PRAGMA key="x\''+key+'\'"' if platform=='wechat' else "PRAGMA key='test-only-key'")
    if platform=='qq':
        db.execute('PRAGMA cipher_page_size=4096')
        db.execute('PRAGMA kdf_iter=4000')
        db.execute('PRAGMA cipher_hmac_algorithm=HMAC_SHA1')
        db.execute('PRAGMA cipher_kdf_algorithm=PBKDF2_HMAC_SHA512')
    db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,text TEXT)')
    db.execute('BEGIN')
    db.executemany('INSERT INTO messages VALUES(?,?)',[(i,'original-'+str(i)+'x'*200) for i in range(1,1501)])
    db.execute('COMMIT')
    db.execute('CREATE INDEX message_text ON messages(text)')
    db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
    target=tmp_path/'plain/messages.db'
    def refresh():
        source=original
        if platform=='qq':
            source=folder/'nt_msg.db';source.write_bytes(bytes(1024)+original.read_bytes())
            wal=Path(str(original)+'-wal');copied=Path(str(source)+'-wal')
            if wal.exists():shutil.copyfile(wal,copied)
            else:copied.unlink(missing_ok=True)
        return incremental.refresh_database(source,target,platform=platform,account='test',key=key)
    yield db,target,refresh
    db.close()


def rows(path):
    db=sqlite3.connect(path)
    try:
        assert db.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
        return db.execute('SELECT id,text FROM messages ORDER BY id').fetchall()
    finally:db.close()


def test_native_incremental_updates_old_rows_deletes_and_appends(fixture):
    source,target,refresh=fixture
    first=refresh();assert first['mode']=='baseline'
    source.execute('BEGIN')
    source.execute("UPDATE messages SET text='edited-old-row' WHERE id=1")
    source.execute('DELETE FROM messages WHERE id=14')
    source.execute("INSERT INTO messages VALUES(9000,'new-row')")
    source.execute('COMMIT')
    second=refresh()
    assert second['mode']=='incremental'
    assert second['pages_written']<second['pages_total']//2
    assert rows(target)==source.execute('SELECT id,text FROM messages ORDER BY id').fetchall()
    third=refresh();assert not third['changed'] and third['pages_written']==0 and third['cipher_bytes_written']==0


def test_failure_rolls_back_both_plaintext_and_page_fingerprints(fixture,monkeypatch):
    source,target,refresh=fixture
    refresh();before=target.read_bytes()
    source.execute("UPDATE messages SET text='changed after baseline' WHERE id=700")
    def fail(stage):
        if stage=='before_commit':raise RuntimeError('injected disk failure')
    with monkeypatch.context() as patch:
        patch.setattr(incremental,'_fault',fail)
        with pytest.raises(RuntimeError,match='injected'):refresh()
    assert target.read_bytes()==before
    result=refresh();assert result['pages_written']>0
    assert rows(target)==source.execute('SELECT id,text FROM messages ORDER BY id').fetchall()


def test_schema_changes_and_vacuum_shrink_preserve_exact_contents(fixture):
    source,target,refresh=fixture
    refresh()
    source.execute('CREATE TABLE other(id INTEGER PRIMARY KEY,body TEXT)')
    source.execute("INSERT INTO other VALUES(5,'new table')")
    source.execute('DELETE FROM messages WHERE id>10')
    source.execute('VACUUM')
    result=refresh();assert result['mode']=='incremental'
    assert rows(target)==source.execute('SELECT id,text FROM messages ORDER BY id').fetchall()
    with NativeSQLite(target) as db:
        assert db.execute('SELECT * FROM other')==[(5,'new table')]


def test_incomplete_baseline_is_rebuilt_without_trusting_old_metadata(fixture):
    source,target,refresh=fixture
    refresh()
    marker=target.parent/'.incremental'/target.name/'baseline-incomplete'
    marker.write_text('interrupted')
    assert refresh()['mode']=='baseline'
    assert not marker.exists()


def test_external_plaintext_changes_force_verified_rebuild(fixture):
    source,target,refresh=fixture
    refresh()
    with sqlite3.connect(target) as db:
        db.execute("UPDATE messages SET text='external edit' WHERE id=1")
    db.close()
    assert refresh()['mode']=='baseline'
    assert rows(target)==source.execute('SELECT id,text FROM messages ORDER BY id').fetchall()


def test_growth_adds_native_pages(fixture):
    source,target,refresh=fixture
    first=refresh()
    source.execute('BEGIN')
    source.executemany('INSERT INTO messages VALUES(?,?)',[(i,'growth'*800) for i in range(9000,9100)])
    source.execute('COMMIT')
    result=refresh()
    assert result['mode']=='incremental' and result['pages_total']>first['pages_total']
    assert rows(target)==source.execute('SELECT id,text FROM messages ORDER BY id').fetchall()


def test_uncommitted_wal_is_not_published(fixture):
    source,target,refresh=fixture
    refresh()
    before=rows(target)
    source.execute('BEGIN')
    source.execute("UPDATE messages SET text='uncommitted' WHERE id=700")
    try:
        assert refresh()['changed'] is False
        assert rows(target)==before
    finally:
        source.execute('ROLLBACK')


def test_corrupt_ciphertext_preserves_verified_plaintext(fixture):
    source,target,refresh=fixture
    source.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    refresh()
    before=target.read_bytes()
    original=target.parent.parent/'source/source.db'
    with original.open('r+b') as stream:
        stream.seek(4096+200)
        value=stream.read(1)
        stream.seek(4096+200)
        stream.write(bytes([value[0]^1]))
    with pytest.raises((RuntimeError,TypeError)):
        refresh()
    assert target.read_bytes()==before


def test_qq_encrypted_page_changes_publish_only_candidate_rows(fixture,monkeypatch):
    source,raw,refresh=fixture
    if source.execute('PRAGMA cipher_hmac_algorithm').fetchone()[0] != 'HMAC_SHA1':
        pytest.skip('QQ-only logical publication path')
    from collector.native_delta import capture_rows
    from collector.qq_incremental import sync_plaintext
    library=Path(__file__).parents[1]/'runtime-deps/native/sqlcipher-qq-pages.dll'
    implementation=incremental.refresh_database
    monkeypatch.setattr(incremental,'refresh_database',lambda *args,**kwargs: implementation(
        *args,**kwargs,library=library,pending_byte=0x3ffffc00,collect_changes=capture_rows))
    refresh()
    public=raw.parent/'public.db'
    sync_plaintext(raw,public,library)
    source.execute('BEGIN')
    source.execute("UPDATE messages SET text='old-edit' WHERE id=2")
    source.execute('DELETE FROM messages WHERE id=1500')
    source.execute("INSERT INTO messages VALUES(9000,'new-message')")
    source.execute('COMMIT')
    delta=refresh()
    result=sync_plaintext(raw,public,library,candidates=delta['_row_candidates'],
                          previous_raw_stamp=delta['_previous_target_stamp'])
    assert result['rows_written']==2 and result['rows_deleted']==2
    assert result['candidate_rows'] < 1500
    assert rows(public)==source.execute('SELECT id,text FROM messages ORDER BY id').fetchall()


def test_process_crash_recovers_last_verified_snapshot(fixture):
    import os
    import subprocess
    import sys
    source,target,refresh=fixture
    refresh()
    expected=rows(target)
    source.execute("UPDATE messages SET text='crash-test' WHERE id BETWEEN 20 AND 700")
    folder=target.parent.parent/'source'
    platform='qq' if source.execute('PRAGMA cipher_hmac_algorithm').fetchone()[0]=='HMAC_SHA1' else 'wechat'
    original=folder/'source.db'
    captured=original
    if platform=='qq':
        captured=folder/'nt_msg.db'
        captured.write_bytes(bytes(1024)+original.read_bytes())
        shutil.copyfile(Path(str(original)+'-wal'),Path(str(captured)+'-wal'))
    child = """
import json,os,sys
from collector import incremental
from collector.native_sqlite import NativeSQLite
original_init=NativeSQLite.__init__
def initialize(self,*args,**kwargs):
    original_init(self,*args,**kwargs)
    if not kwargs.get('readonly'): self.execute('PRAGMA cache_size=1')
NativeSQLite.__init__=initialize
incremental._fault=lambda stage: os._exit(73) if stage=='before_commit' else None
incremental.refresh_database(sys.argv[1],sys.argv[2],platform=sys.argv[3],account='test',
    key='test-only-key' if sys.argv[3]=='qq' else 'a1'*32)
"""
    result=subprocess.run([sys.executable,'-c',child,str(captured),str(target),platform],
        capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert result.returncode==73, result.stderr.decode(errors='replace')
    assert rows(target)==expected
    refresh()
    assert rows(target)==source.execute('SELECT id,text FROM messages ORDER BY id').fetchall()


def _capture_wal_fixture(tmp_path):
    live_path=tmp_path/'live.db'
    live=sqlite3.connect(live_path,isolation_level=None)
    try:
        live.execute('CREATE TABLE proof(value TEXT)')
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('PRAGMA wal_autocheckpoint=0')
        live.execute("INSERT INTO proof VALUES('committed')")
        source=tmp_path/'source.db'
        source.write_bytes(live_path.read_bytes())
        committed=Path(str(live_path)+'-wal').read_bytes()
        wal=Path(str(source)+'-wal')
        wal.write_bytes(committed+bytes(2*(4096+24)))
        return source,wal,committed
    finally:
        live.close()


def test_capture_ignores_changing_preallocated_wal_tail_without_rewriting_it(tmp_path,monkeypatch):
    source,wal,committed=_capture_wal_fixture(tmp_path)
    original=incremental._hash_pages
    calls=0
    def hash_and_append(*args):
        nonlocal calls
        calls+=1
        with wal.open('r+b') as stream:
            stream.seek(len(committed)+2)
            stream.write(bytes([calls]))
        return original(*args)
    monkeypatch.setattr(incremental,'_hash_pages',hash_and_append)
    mirror=tmp_path/'private/cipher.db'
    _,captured,written=incremental.capture(source,mirror)
    assert captured==committed
    assert Path(str(mirror)+'-wal').read_bytes()==committed
    assert calls==1 and written>0
    assert incremental.capture(source,mirror)[2]==0
    assert calls==2


@pytest.mark.parametrize('phase',['copy','verify'])
def test_capture_rejects_changes_inside_committed_wal_prefix(tmp_path,monkeypatch,phase):
    source,wal,committed=_capture_wal_fixture(tmp_path)
    def alter_committed_page():
        with wal.open('r+b') as stream:
            stream.seek(32+24+100)
            value=stream.read(1)
            stream.seek(32+24+100)
            stream.write(bytes([value[0]^1]))
    if phase=='copy':
        original=incremental._copy_changed
        def changed_copy(path,*args,**kwargs):
            if path==wal:alter_committed_page()
            return original(path,*args,**kwargs)
        monkeypatch.setattr(incremental,'_copy_changed',changed_copy)
    else:
        original=incremental._hash_pages
        def changed_hash(*args):
            alter_committed_page()
            return original(*args)
        monkeypatch.setattr(incremental,'_hash_pages',changed_hash)
    with pytest.raises(RuntimeError,match='snapshot_source_changed_during_capture'):
        incremental.capture(source,tmp_path/'private/cipher.db')


def test_capture_still_retries_three_times_when_main_pages_change(tmp_path,monkeypatch):
    source,wal,committed=_capture_wal_fixture(tmp_path)
    original=incremental._hash_pages
    calls=0
    def changed_hash(*args):
        nonlocal calls
        calls+=1
        with source.open('r+b') as stream:
            stream.seek(100)
            value=stream.read(1)
            stream.seek(100)
            stream.write(bytes([value[0]^1]))
        return original(*args)
    monkeypatch.setattr(incremental,'_hash_pages',changed_hash)
    with pytest.raises(RuntimeError,match='snapshot_source_changed_during_capture'):
        incremental.capture(source,tmp_path/'private/cipher.db')
    assert calls==3
