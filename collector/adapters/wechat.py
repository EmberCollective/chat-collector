"""Refresh validated WeChat snapshots through the preserved native engine."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from ..vendor.wechat.decrypt import WeChatDatabaseDecryptor

_TABLE = re.compile(r"^Msg_[0-9a-fA-F]{32}$")


def _source(config):
    return config.get("source", config)


def _fingerprint(path):
    result = []
    for suffix in ("", "-wal", "-journal"):
        part = Path(str(path) + suffix)
        try:
            stat = part.stat()
            result.append([suffix, stat.st_size, stat.st_mtime_ns, stat.st_ino])
        except FileNotFoundError:
            result.append([suffix, None])
    return result


def _index_snapshot(path):
    connection = sqlite3.connect(path)
    try:
        # DDL otherwise commits each index separately, causing one disk sync per conversation.
        connection.execute("BEGIN")
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'") if _TABLE.fullmatch(r[0])]
        for table in tables:
            connection.execute(f'CREATE INDEX IF NOT EXISTS "_collector_time_{table[4:]}" ON "{table}"(create_time,local_id)')
        connection.commit()
    finally:
        connection.close()


def refresh(config):
    """Decrypt only changed source DB/WAL snapshots through preserved upstream code."""
    started = time.perf_counter()
    source = _source(config)
    if not source.get("encrypted_dir"):
        return {"updated": 0, "mode": "decrypted_only"}
    root, destination = Path(source["encrypted_dir"]).resolve(), Path(source["decrypted_dir"]).resolve()
    if root == destination or root in destination.parents or destination in root.parents:
        raise ValueError("Encrypted and decrypted database directories must be separate")
    account = str(source["account_id"])
    store = json.loads(Path(source["key_file"]).read_text(encoding="utf-8"))
    key = store[account]["db_key"]
    decryptor = WeChatDatabaseDecryptor(key)
    files = sorted(p for p in root.glob("message/message_*.db") if re.fullmatch(r"message_\d+\.db", p.name))
    if not files:
        raise FileNotFoundError("No encrypted WeChat message/message_*.db databases found")
    if (root / 'contact/contact.db').is_file():
        files.append(root / 'contact/contact.db')
    if source.get("resource_db") and Path(source["resource_db"]).resolve() == destination / "message_resource.db":
        for relative in ("message/message_resource.db", "hardlink/hardlink.db"):
            if (root / relative).is_file():
                files.append(root / relative)
    destination.mkdir(parents=True, exist_ok=True)
    if source.get('snapshot_mode') == 'incremental':
        from ..incremental import refresh_database
        validation = [refresh_database(database, destination/database.name,
            platform='wechat', account=account, key=key, work_dir=source.get('work_dir')) for database in files]
        return {'updated': sum(item['changed'] for item in validation), 'databases': len(files),
                'validation': validation, 'timings_seconds': {'total': round(time.perf_counter()-started, 3)}}
    manifest = destination / ".refresh.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    current = {}
    updated = 0
    validation = []
    for database in files:
        fingerprint = {"source": str(database), "files": _fingerprint(database), "key_digest": hashlib.sha256(bytes.fromhex(key)).hexdigest()}
        target = destination / database.name
        if previous.get(database.name) != fingerprint or not target.exists():
            phase = time.perf_counter()
            if not decryptor.decrypt_database(str(database), str(target)):
                raise RuntimeError(f"WeChat refresh failed for {database.name}: {decryptor.last_result.get('error', 'unknown error')}")
            decrypt_seconds = time.perf_counter() - phase
            phase = time.perf_counter()
            _index_snapshot(target)
            validation.append({"database": database.name,
                               "quick_check_ok": decryptor.last_result.get("diagnostics", {}).get("quick_check_ok"),
                               "hmac_warning_pages": decryptor.last_result.get("hmac_warning_pages", 0),
                               "timings_seconds": {"decrypt_and_validate": round(decrypt_seconds, 3),
                                                   "time_indexes": round(time.perf_counter()-phase, 3)}})
            updated += 1
        current[database.name] = fingerprint
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination, delete=False) as stream:
        json.dump(current, stream, sort_keys=True)
        temp = Path(stream.name)
    temp.replace(manifest)
    return {"updated": updated, "databases": len(files), "validation": validation,
            "timings_seconds": {"total": round(time.perf_counter()-started, 3)}}

def scan(config, window_start, window_end, where_sql="1", where_params=None, *, context=None, conversations=None):
    """Legacy query entry; collection refresh does not load the query provider."""
    from providers.platforms.wechat import scan as provider_scan
    return provider_scan(config, window_start, window_end, where_sql, where_params,
                         context=context, conversations=conversations)


def __getattr__(name):
    # Preserve legacy decoding helpers without loading compatibility during refresh.
    if name in {'decode_text', '_bounded_blob', '_xml', '_visible_text', '_mentions_all',
                '_kind', '_attachments', '_connect'}:
        from providers.platforms import wechat
        return getattr(wechat, name)
    raise AttributeError(name)
