"""Refresh validated NTQQ snapshots with the preserved native engine."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time



def _prefix_digest(stream, size, destination=None):
    """Read a fixed prefix; concurrent appends do not move the capture boundary."""
    digest = hashlib.sha256()
    while size:
        chunk = stream.read(min(size, 8 * 1024 * 1024))
        if not chunk:
            raise RuntimeError("QQ snapshot input shrank during capture")
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
        size -= len(chunk)
    return digest.digest()


def _capture_snapshot(source, stripped, stamp):
    """Keep a stable main DB and WAL prefix, allowing later WAL appends.

    SQLite itself validates WAL salts, frame checksums and transaction commit
    boundaries when opening the copy. An incomplete final transaction is ignored
    by that native recovery; we do not implement a replacement WAL parser.
    """
    wal = Path(str(source) + "-wal")
    copied_wal = Path(str(stripped) + "-wal")
    for attempt in range(3):
        before = stamp()
        if before[0][1] <= 1024:
            raise ValueError("The NTQQ source has no encrypted database pages")
        with source.open("rb") as src, stripped.open("wb") as dst:
            src.seek(1024)
            shutil.copyfileobj(src, dst, length=8 * 1024 * 1024)
        copied_wal.unlink(missing_ok=True)
        try:
            captured = stamp()
            if before[0] != captured[0]:
                continue
            if captured[1][1] is not None:
                cutoff = captured[1][1]
                with wal.open("rb") as src, copied_wal.open("wb") as dst:
                    expected = _prefix_digest(src, cutoff, dst)
                with wal.open("rb") as src:
                    if _prefix_digest(src, cutoff) != expected:
                        continue
            after = stamp()
            if captured[0] != after[0]:
                continue
            if captured[1][1] is None and after[1][1] is not None:
                continue
            if captured[1][1] is not None and (after[1][1] is None or after[1][1] < captured[1][1]):
                continue
            return captured
        except (FileNotFoundError, RuntimeError):
            continue
    raise RuntimeError("QQ checkpointed or replaced its database during all snapshot attempts; retry next scan")


def _export_tables(conn, plain):
    """Use native SQLCipher reads; rebuild SQLite objects without its export helper."""
    schema = conn.execute("SELECT type,name,sql FROM main.sqlite_master WHERE sql IS NOT NULL").fetchall()
    quote = lambda value: '"' + value.replace('"', '""') + '"'
    conn.execute("ATTACH DATABASE ? AS plaintext KEY ''", (str(plain),))
    counts = {}
    conn.execute("BEGIN")
    try:
        for kind, name, sql in schema:
            if kind != "table" or name.startswith("sqlite_"):
                continue
            if not re.match(r"CREATE TABLE\s+", sql, re.I):
                raise ValueError("Unsupported QQ virtual table: " + name)
            conn.execute(re.sub(r"^CREATE TABLE\s+", "CREATE TABLE plaintext.", sql, count=1, flags=re.I))
            columns = [row[1] for row in conn.execute("PRAGMA main.table_info(" + quote(name) + ")")]
            if not re.search(r"\bWITHOUT\s+ROWID\b", sql, re.I) and not any(c.lower() in {"rowid", "_rowid_", "oid"} for c in columns):
                columns.insert(0, "rowid")
            names = ','.join(map(quote, columns))
            conn.execute(f"INSERT INTO plaintext.{quote(name)} ({names}) SELECT {names} FROM main.{quote(name)} NOT INDEXED")
            # INSERT reports its source rows; avoid decrypting the whole table again to count.
            expected = conn.execute("SELECT changes()").fetchone()[0]
            if conn.execute(f"SELECT count(*) FROM plaintext.{quote(name)}").fetchone()[0] != expected:
                raise RuntimeError("QQ table export count mismatch: " + name)
            counts[name] = expected
        if any(name == "sqlite_sequence" for _, name, _ in schema):
            conn.execute("DELETE FROM plaintext.sqlite_sequence")
            conn.execute("INSERT INTO plaintext.sqlite_sequence SELECT * FROM main.sqlite_sequence")
        for kind, name, sql in schema:
            if kind in {"index", "view", "trigger"} and not name.startswith("sqlite_"):
                pattern = r"^(CREATE\s+(?:UNIQUE\s+)?" + kind + r"\s+)"
                conn.execute(re.sub(pattern, r"\1plaintext.", sql, count=1, flags=re.I))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("DETACH DATABASE plaintext")
    return counts


def _publish_snapshot(plain, destination):
    """Cross-volume publication is copied and verified before replacing the good snapshot."""
    try:
        plain.replace(destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV and getattr(exc, 'winerror', None) != 17:
            raise
    staging = None
    try:
        with plain.open('rb') as source, tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix='.qq-publish-', suffix='.db', delete=False) as target:
            staging = Path(target.name)
            expected = _prefix_digest(source, plain.stat().st_size, target)
            target.flush()
            os.fsync(target.fileno())
        with staging.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').digest() != expected:
                raise RuntimeError('QQ snapshot copy hash mismatch')
        staging.replace(destination)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def refresh(config):
    """Export a stable NTQQ DB+WAL copy through the official SQLCipher engine."""
    started = time.perf_counter()
    timings = {}
    import sqlcipher3.dbapi2 as sqlcipher

    source = (Path(config["encrypted_dir"]) / "nt_msg.db").resolve(strict=True)
    destination = Path(config["decrypted_dir"]) / "nt_msg.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    if source == destination:
        raise ValueError("The decrypted destination must not replace the source database")
    key_data = json.loads(Path(config["key_file"]).read_text(encoding="utf-8"))
    if str(key_data.get("account")) != str(config["account_id"]):
        raise ValueError("The QQ key file belongs to a different account")
    key = str(key_data["key"])
    if not key:
        raise ValueError("The QQ database key is empty")
    key_mode = key_data.get("key_mode", "passphrase")
    if key_mode not in ("passphrase", "raw_key_salt"):
        raise ValueError("Unsupported QQ key_mode")
    if key_mode == "raw_key_salt" and not re.fullmatch(r"[0-9a-fA-F]{96}", key):
        raise ValueError("QQ raw_key_salt must contain a 32-byte key and 16-byte salt as hex")

    if config.get('snapshot_mode') == 'incremental':
        from ..incremental import refresh_database
        from ..native_delta import capture_rows
        from ..qq_incremental import sync_plaintext
        library = Path(__file__).resolve().parents[2]/'runtime-deps/native/sqlcipher-qq-pages.dll'
        raw = destination.parent/'.incremental/qq-native.db'
        pages = refresh_database(source, raw, platform='qq', account=config['account_id'],
            key=key, key_mode=key_mode, work_dir=config.get('work_dir'), library=library,
            pending_byte=0x40000000-1024, collect_changes=capture_rows)
        candidates=pages.pop('_row_candidates',None)
        previous_stamp=pages.pop('_previous_target_stamp',None)
        logical=sync_plaintext(raw,destination,library,candidates=candidates,previous_raw_stamp=previous_stamp)
        return {'changed':logical['changed'],'database':str(destination),'mode':'incremental',
                'page_refresh':pages,'logical_sync':logical,
                'timings_seconds':{'total':round(time.perf_counter()-started,3)}}

    wal = Path(str(source) + "-wal")
    marker = destination.with_suffix(".source.json")

    def stamp():
        entries = []
        for path in (source, wal):
            try:
                stat = path.stat()
                entries.append([str(path), stat.st_size, stat.st_mtime_ns])
            except FileNotFoundError:
                entries.append([str(path), None, None])
        return entries

    before = stamp()
    if destination.exists() and marker.exists() and json.loads(marker.read_text(encoding="utf-8")) == before:
        return {"changed": False, "database": str(destination),
                "timings_seconds": {"total": round(time.perf_counter()-started, 3)}}
    work_dir = Path(config.get('work_dir') or destination.parent).resolve()
    if work_dir == source.parent or source.parent in work_dir.parents:
        raise ValueError('QQ work_dir must be outside the original encrypted directory')
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qq-refresh-", dir=work_dir) as temp:
        stripped = Path(temp) / "encrypted.db"
        plain = Path(temp) / "plain.db"
        phase = time.perf_counter()
        before = _capture_snapshot(source, stripped, stamp)
        timings['capture'] = round(time.perf_counter()-phase, 3)

        phase = time.perf_counter()
        conn = sqlcipher.connect(str(stripped), isolation_level=None)
        try:
            conn.execute("PRAGMA cipher_page_size=4096")
            if key_mode == "raw_key_salt":
                conn.execute('PRAGMA key="x\'' + key + '\'"')
            else:
                conn.execute("PRAGMA key='" + key.replace("'", "''") + "'")
            conn.execute("PRAGMA kdf_iter=4000")
            conn.execute("PRAGMA cipher_hmac_algorithm=HMAC_SHA1")
            conn.execute("PRAGMA cipher_kdf_algorithm=PBKDF2_HMAC_SHA512")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            counts = _export_tables(conn, plain)
        finally:
            conn.close()
        timings['export'] = round(time.perf_counter()-phase, 3)
        check = sqlite3.connect(plain)
        try:
            phase = time.perf_counter()
            if check.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("The exported QQ snapshot failed SQLite integrity validation")
            timings['integrity_check'] = round(time.perf_counter()-phase, 3)
            phase = time.perf_counter()
            for table in ("group_msg_table", "c2c_msg_table"):
                if table in counts:
                    check.execute(f'CREATE INDEX IF NOT EXISTS "_collector_time_{table}" ON "{table}"("40050")')
            check.commit()
            timings['time_indexes'] = round(time.perf_counter()-phase, 3)
        finally:
            check.close()
        phase = time.perf_counter()
        _publish_snapshot(plain, destination)
        timings['publish'] = round(time.perf_counter()-phase, 3)
        marker_temp = Path(temp) / "source.json"
        marker_temp.write_text(json.dumps(before), encoding="utf-8")
        _publish_snapshot(marker_temp, marker)
    timings['total'] = round(time.perf_counter()-started, 3)
    return {"changed": True, "database": str(destination), "tables": counts,
            "quick_check_ok": True, "timings_seconds": timings}


def scan(config, window_start, window_end, where_sql="1", where_params=None, *, context=None, conversations=None):
    """Legacy query entry; collection refresh does not load the query provider."""
    from providers.platforms.qq import scan as provider_scan
    return provider_scan(config, window_start, window_end, where_sql, where_params,
                         context=context, conversations=conversations)


def __getattr__(name):
    if name == '_body':
        from providers.platforms.qq import _body
        return _body
    raise AttributeError(name)
