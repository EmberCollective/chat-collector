"""Verified encrypted page fingerprints drive native, transactional plaintext updates."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from .native_sqlite import NativeSQLite
from .vendor.wechat.sqlite_wal import committed_wal_pages

PAGE_SIZE = 4096
ZERO_HASH = hashlib.sha256(bytes(PAGE_SIZE)).digest()


def _stamp(path):
    try:
        stat=path.stat()
        return stat.st_size,stat.st_mtime_ns,stat.st_ino
    except FileNotFoundError:
        return None


def _hot_journal(path):
    try:
        with path.open('rb') as stream:
            return stream.read(8)==bytes.fromhex('d9d505f920a163d7')
    except FileNotFoundError:
        return False


def _copy_changed(source, target, *, offset=0, length=None):
    """Read sequentially; write only differing pages into the private cipher mirror."""
    hashes=[];written=0
    with source.open('rb') as original, target.open('r+b' if target.exists() else 'w+b') as mirror:
        original.seek(offset)
        remaining=length if length is not None else source.stat().st_size-offset
        position=0
        while remaining:
            block=original.read(min(1024*1024,remaining))
            if not block:
                raise RuntimeError('snapshot_source_shrank')
            mirror.seek(position);old=mirror.read(len(block))
            for index in range(0,len(block),PAGE_SIZE):
                page=block[index:index+PAGE_SIZE]
                hashes.append(hashlib.sha256(page).digest())
                if page!=old[index:index+PAGE_SIZE]:
                    mirror.seek(position+index);mirror.write(page);written+=len(page)
            position+=len(block);remaining-=len(block)
        mirror.truncate(position);mirror.flush();os.fsync(mirror.fileno())
    return hashes,written


def _hash_pages(source,offset,length):
    hashes=[]
    with source.open('rb') as stream:
        stream.seek(offset)
        while length:
            block=stream.read(min(1024*1024,length))
            if not block:raise RuntimeError('snapshot_source_shrank')
            hashes.extend(hashlib.sha256(block[i:i+PAGE_SIZE]).digest() for i in range(0,len(block),PAGE_SIZE))
            length-=len(block)
    return hashes


def capture(source,mirror,offset=0):
    """Validate two main-file reads and a fixed WAL prefix; later appends wait for next round."""
    wal=Path(str(source)+'-wal');copied_wal=Path(str(mirror)+'-wal')
    mirror.parent.mkdir(parents=True,exist_ok=True)
    for suffix in ('-shm','-journal'):
        Path(str(mirror)+suffix).unlink(missing_ok=True)
    written=0
    for attempt in range(3):
        before=_stamp(source)
        if before is None or before[0]<=offset or (before[0]-offset)%PAGE_SIZE:
            raise ValueError('snapshot_source_pages_invalid')
        journal=Path(str(source)+'-journal')
        if _hot_journal(journal):
            raise RuntimeError('snapshot_source_has_hot_journal')
        try:
            hashes,size=_copy_changed(source,mirror,offset=offset,length=before[0]-offset);written+=size
            wal_stamp=_stamp(wal)
            if wal_stamp:
                with wal.open('rb') as stream:wal_data=stream.read(wal_stamp[0])
                if len(wal_data)!=wal_stamp[0]:continue
                _,_,info=committed_wal_pages(wal_data,PAGE_SIZE,len(hashes))
                # Preallocated tail frames may change while the committed snapshot stays valid.
                cutoff=32+info['committed_frames']*(PAGE_SIZE+24)
                wal_data=wal_data[:cutoff]
                _,size=_copy_changed(wal,copied_wal,length=len(wal_data));written+=size
                if copied_wal.read_bytes()!=wal_data:continue
            else:
                copied_wal.unlink(missing_ok=True);wal_data=b''
            after_hashes=_hash_pages(source,offset,before[0]-offset)
            after=_stamp(source)
            if after_hashes!=hashes or after is None or (after[0],after[2])!=(before[0],before[2]):
                continue
            if wal_stamp:
                with wal.open('rb') as stream:
                    if stream.read(len(wal_data))!=wal_data:continue
            elif wal.exists():continue
            if _hot_journal(journal):continue
            return hashes,wal_data,written
        except (FileNotFoundError,RuntimeError,ValueError):
            continue
    raise RuntimeError('snapshot_source_changed_during_capture')


def _plain_page(page,number,count):
    if number!=1:return page
    data=bytearray(page);data[18:20]=b'\x01\x01';data[28:32]=count.to_bytes(4,'big')
    data[92:96]=data[24:28]
    return bytes(data)


def _verify(db, *, full=True):
    result=db.execute('PRAGMA integrity_check' if full else 'PRAGMA quick_check')
    if result!=[('ok',)]:
        raise RuntimeError('incremental_snapshot_integrity_failed: '+str(result[:2])[:500])


def _fault(stage):
    """Injection point for crash/rollback regression tests."""


def _failure_kind(exc):
    message=str(exc).lower()
    if isinstance(exc,PermissionError) or any(value in message for value in ('readonly','permission denied','authorization denied','unable to open')):
        return 'permission'
    if 'key_mismatch' in message or 'file is not a database' in message:return 'key_mismatch'
    if 'snapshot_source' in message:return 'capture'
    if any(value in message for value in ('disk is full','disk i/o','disk failure','disk space')) or isinstance(exc,OSError):
        return 'io'
    if message.startswith('native_sqlite: database disk image is malformed'):return 'malformed'
    if message.startswith('incremental_snapshot_integrity_failed:'):return 'integrity'
    return 'other'


def refresh_database(source,target,*,platform,account,key,key_mode='passphrase',work_dir=None,library=None,pending_byte=None,collect_changes=None):
    diagnostic={'stage':'prepare','page':None,'rollback':'not_started'}
    try:
        return _refresh_database(source,target,platform=platform,account=account,key=key,key_mode=key_mode,
            work_dir=work_dir,library=library,pending_byte=pending_byte,collect_changes=collect_changes,
            diagnostic=diagnostic)
    except Exception as exc:
        # Native errors may contain SQL/schema details; expose only fixed categories and location.
        detail=' '.join(f'{name}={value}' for name,value in diagnostic.items())
        message=f"snapshot_refresh_failed database={Path(target).name} category={_failure_kind(exc)} {detail}"
        raise (ValueError(message) if isinstance(exc,ValueError) else RuntimeError(message)) from None


def _refresh_database(source,target,*,platform,account,key,key_mode,work_dir,library,pending_byte,collect_changes,diagnostic):
    started=time.perf_counter()
    source,target=Path(source).resolve(),Path(target).resolve()
    if source==target:raise ValueError('Snapshot cannot replace the source database')
    target.parent.mkdir(parents=True,exist_ok=True)
    private=target.parent/'.incremental'/target.name
    private.mkdir(parents=True,exist_ok=True)
    # Only collector-owned, per-database staging files; callers hold InstanceLock.
    for pattern in ('baseline-*.db', 'truncate-check-*.db'):
        for stale in private.glob(pattern):stale.unlink()
    cache=(Path(work_dir).resolve()/('pages-'+platform+'-'+hashlib.sha256(str(source).encode()).hexdigest()[:16])
           if work_dir else private)
    if cache==source.parent or source.parent in cache.parents:
        raise ValueError('Incremental cache must be outside the source database directory')
    cache.mkdir(parents=True,exist_ok=True)
    mirror=cache/'cipher.db';state=private/'page-state.sqlite';dirty=private/'baseline-incomplete'
    phase=time.perf_counter()
    diagnostic['stage']='capture'
    hashes,wal,written=capture(source,mirror,1024 if platform=='qq' else 0)
    committed,base_pages,wal_info=committed_wal_pages(wal,PAGE_SIZE,len(hashes))
    count=wal_info['database_pages']
    current=hashes[:base_pages]+[None]*(count-base_pages)
    for number,page in committed.items():current[number-1]=hashlib.sha256(page).digest()
    capture_seconds=time.perf_counter()-phase
    del hashes,wal,committed
    native_options={'library':library, 'pending_byte':pending_byte}
    diagnostic['stage']='source_open'
    with NativeSQLite(mirror,readonly=True,**native_options) as encrypted:
        diagnostic['stage']='key'
        if platform=='wechat':
            from .vendor.wechat.decrypt import _resolve_page1_key_material
            with mirror.open('rb') as stream:page1=stream.read(PAGE_SIZE)
            if page1.startswith(b'SQLite format 3\x00'):
                material=None
            else:
                material=_resolve_page1_key_material(bytes.fromhex(key),page1)
                if material is None:raise ValueError('key_mismatch')
                encrypted.execute('PRAGMA key="x\''+material[0].hex()+'\'"')
        elif key_mode=='raw_key_salt':
            encrypted.execute('PRAGMA key="x\''+key+'\'"')
        else:
            encrypted.execute("PRAGMA key='"+key.replace("'","''")+"'")
        if platform=='qq':
            encrypted.execute('PRAGMA cipher_page_size=4096')
            encrypted.execute('PRAGMA kdf_iter=4000')
            encrypted.execute('PRAGMA cipher_hmac_algorithm=HMAC_SHA1')
            encrypted.execute('PRAGMA cipher_kdf_algorithm=PBKDF2_HMAC_SHA512')
        encrypted.execute('PRAGMA query_only=ON')
        encrypted.execute('BEGIN')
        diagnostic['stage']='source_page_map'
        header=encrypted.execute('SELECT data FROM sqlite_dbpage WHERE pgno=1')[0][0]
        logical=int.from_bytes(header[28:32],'big')
        if (0<logical<count and header[24:28]==header[92:96]
                and all(value==ZERO_HASH for value in current[logical:])):
            count=logical;current=current[:count]
        if not wal_info['committed_frames']:
            count=min(count,encrypted.execute('PRAGMA page_count')[0][0]);current=current[:count]
        if count<1 or any(value is None for value in current):
            raise RuntimeError('snapshot_page_map_incomplete')
        identity=hashlib.sha256(json.dumps([1,str(source),platform,str(account),key,key_mode,header[20],PAGE_SIZE]).encode()).hexdigest()
        diagnostic['stage']='metadata'
        with closing(sqlite3.connect(state)) as metadata, metadata:
            metadata.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            metadata.execute('CREATE TABLE IF NOT EXISTS pages(pgno INTEGER PRIMARY KEY,hash BLOB NOT NULL)')
            old_identity=metadata.execute("SELECT value FROM settings WHERE key='identity'").fetchone()
            target_stamp=metadata.execute("SELECT value FROM settings WHERE key='target_stamp'").fetchone()
            previous=dict(metadata.execute('SELECT pgno,hash FROM pages'))
        diagnostic['stage']='target_recovery'
        if Path(str(target)+'-journal').exists():
            with NativeSQLite(target,**native_options) as recovery:
                recovery.execute('SELECT count(*) FROM sqlite_master')
        full=(dirty.exists() or not target.exists() or old_identity!=(identity,)
              or target_stamp!=(json.dumps(_stamp(target)),))
        previous_target_stamp=_stamp(target)
        diagnostic.update(pages_before=len(previous),pages_after=count,
                          schema_before=None,schema_after=int.from_bytes(header[40:44],'big'))
        if target.exists():
            with target.open('rb') as stream:old_header=stream.read(100)
            diagnostic['schema_before']=int.from_bytes(old_header[40:44],'big')
        candidates=None
        changed=list(range(1,count+1)) if full else [n for n,h in enumerate(current,1) if previous.get(n)!=h]
        truncated=len(previous)>count
        if not changed and not truncated:
            return {'changed':False,'mode':'incremental','pages_decrypted':1,'pages_written':0,
                    'pages_total':count,'cipher_bytes_written':written,'database':str(target),
                    'timings_seconds':{'capture':round(capture_seconds,3),'total':round(time.perf_counter()-started,3)}}
        phase=time.perf_counter()
        recovery=None
        if not full:
            try:
                diagnostic['stage']='transaction_setup'
                with NativeSQLite(target,**native_options) as destination:
                    if destination.execute('PRAGMA journal_mode=DELETE')!=[('delete',)]:
                        raise RuntimeError('incremental_snapshot_requires_rollback_journal')
                    destination.execute('PRAGMA synchronous=FULL')
                    destination.execute("ATTACH DATABASE ? AS page_state KEY ''",(str(state),))
                    destination.execute('PRAGMA page_state.journal_mode=DELETE')
                    destination.execute('PRAGMA page_state.synchronous=FULL')
                    destination.execute('BEGIN IMMEDIATE')
                    diagnostic['rollback']='pending'
                    try:
                        diagnostic['stage']='read_previous'
                        if collect_changes:
                            dirty_pages=set(changed)|set(range(count+1,len(previous)+1))
                            old_schema=destination.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')
                            before_rows=collect_changes(destination, dirty_pages)
                        resized=count!=len(previous)
                        image=None
                        if resized:
                            image=bytearray(count*PAGE_SIZE)
                            for number,page in destination.iterate('SELECT pgno,data FROM sqlite_dbpage WHERE pgno<=?',(count,)):
                                image[(number-1)*PAGE_SIZE:number*PAGE_SIZE]=page
                        # Publish the new schema header only after its referenced pages are present.
                        for number in sorted(set(changed)-{1})+[1]:
                            diagnostic.update(stage='read_page',page=number)
                            page=encrypted.execute('SELECT data FROM sqlite_dbpage WHERE pgno=?',(number,))[0][0]
                            page=_plain_page(page,number,count)
                            if image is not None:image[(number-1)*PAGE_SIZE:number*PAGE_SIZE]=page
                            diagnostic['stage']='write_page'
                            destination.execute('INSERT INTO sqlite_dbpage(pgno,data) VALUES(?,?)',
                                                (number,page))
                            diagnostic['stage']='write_metadata'
                            destination.execute('INSERT OR REPLACE INTO page_state.pages VALUES(?,?)',(number,current[number-1]))
                        diagnostic.update(stage='truncate',page=count+1 if truncated else None)
                        if truncated:destination.execute('INSERT INTO sqlite_dbpage(pgno,data) VALUES(?,NULL)',(count+1,))
                        diagnostic['stage']='write_metadata'
                        destination.execute('DELETE FROM page_state.pages WHERE pgno>?',(count,))
                        diagnostic.update(stage='schema_reload',page=None)
                        destination.execute('PRAGMA writable_schema=RESET')
                        diagnostic['stage']='verify'
                        _fault('before_integrity_check')
                        if image is not None:
                            # dbpage changes the pager size at commit; check the prospective image in RAM.
                            with NativeSQLite(':memory:',**native_options) as checked:
                                checked.deserialize(image)
                                _verify(checked,full=platform!='qq')
                                if collect_changes:
                                    after_rows=collect_changes(checked, dirty_pages)
                                    new_schema=checked.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')
                        else:
                            _verify(destination,full=platform!='qq')
                            if collect_changes:
                                after_rows=collect_changes(destination, dirty_pages)
                                new_schema=destination.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')
                        if collect_changes and old_schema==new_schema:
                            from .native_delta import merge_candidates
                            candidates=merge_candidates(before_rows,after_rows)
                        diagnostic['stage']='commit'
                        _fault('before_commit')
                        destination.execute('COMMIT')
                        diagnostic['rollback']='not_needed'
                    except BaseException:
                        try:
                            destination.execute('ROLLBACK')
                        except BaseException:
                            diagnostic['rollback']='failed'
                            raise
                        diagnostic['rollback']='complete'
                        raise
            except RuntimeError as exc:
                if (platform!='wechat' or diagnostic['rollback']!='complete'
                        or diagnostic['stage'] not in {'write_page','truncate','schema_reload','verify'}
                        or _failure_kind(exc) not in {'malformed','integrity'}):
                    raise
                recovery={**diagnostic,'reason':_failure_kind(exc),'attempts':1}
                diagnostic.update(stage='source_validation',page=None,recovery_attempted=True)
                _verify(encrypted)
                recovery['source_validation']='integrity_check'
                full=True
                candidates=None
        if full:
            diagnostic.update(stage='baseline_build',page=None)
            with dirty.open('wb') as stream:
                stream.write(b'baseline publication pending');stream.flush();os.fsync(stream.fileno())
            temporary=None
            try:
                with tempfile.NamedTemporaryFile(dir=private,prefix='baseline-',suffix='.db',delete=False) as stream:
                    temporary=Path(stream.name)
                    last=0
                    for number,page in encrypted.iterate('SELECT pgno,data FROM sqlite_dbpage WHERE pgno<=? ORDER BY pgno',(count,)):
                        diagnostic['page']=number
                        if number!=last+1 or not isinstance(page,bytes) or len(page)!=PAGE_SIZE:
                            raise RuntimeError('native_snapshot_page_missing')
                        stream.write(_plain_page(page,number,count));last=number
                    if last!=count:raise RuntimeError('native_snapshot_page_missing')
                    stream.flush();os.fsync(stream.fileno())
                diagnostic.update(stage='baseline_verify',page=None)
                with NativeSQLite(temporary,**native_options) as checked:_verify(checked)
                diagnostic['stage']='baseline_publish'
                _fault('before_baseline_publish')
                if any(Path(str(target)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
                    raise RuntimeError('snapshot_target_has_active_sidecar')
                temporary.replace(target)
                with closing(sqlite3.connect(state)) as metadata, metadata:
                    metadata.execute('DELETE FROM pages')
                    metadata.executemany('INSERT INTO pages VALUES(?,?)',enumerate(current,1))
                    metadata.execute("INSERT OR REPLACE INTO settings VALUES('identity',?)",(identity,))
                dirty.unlink()
            finally:
                if temporary:temporary.unlink(missing_ok=True)
        diagnostic.update(stage='metadata_publish',page=None)
        with closing(sqlite3.connect(state)) as metadata, metadata:
            metadata.execute("INSERT OR REPLACE INTO settings VALUES('target_stamp',?)",(json.dumps(_stamp(target)),))
        return {'changed':True,'mode':'baseline' if full else 'incremental','database':str(target),
                **({'recovery':recovery} if recovery else {}),
                **({'_row_candidates':candidates,'_previous_target_stamp':previous_target_stamp} if collect_changes else {}),
                'pages_total':count,'pages_decrypted':count+1 if full else len(set(changed)|{1})+1,'pages_written':count if full else len(set(changed)|{1}),
                'cipher_bytes_written':written,'quick_check_ok':True,
                'validation':'integrity_check' if full or platform!='qq' else 'quick_check',
                'timings_seconds':{'capture':round(capture_seconds,3),'update_and_verify':round(time.perf_counter()-phase,3),
                                   'total':round(time.perf_counter()-started,3)}}
