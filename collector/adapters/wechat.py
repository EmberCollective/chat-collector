"""WeChat 4.x offline refresh and read-only, SQL-filtered message collection."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from ..vendor.wechat.decrypt import WeChatDatabaseDecryptor
from ..vendor.wechat.message_text import _decode_message_content, _decompress_limited, MAX_MESSAGE_BYTES

_ZSTD = b"\x28\xb5\x2f\xfd"
_TABLE = re.compile(r"^Msg_[0-9a-fA-F]{32}$")


def _bounded_blob(value):
    if isinstance(value, (str, bytes, bytearray, memoryview)) and len(value) > MAX_MESSAGE_BYTES:
        raise ValueError("WeChat message exceeds the 16 MiB decoding limit")
    if isinstance(value, str):
        if re.fullmatch(r"[0-9a-fA-F]+", value) and len(value) % 2 == 0:
            candidate = bytes.fromhex(value)
        elif len(value) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/=]+", value):
            try:
                candidate = base64.b64decode(value, validate=True)
            except ValueError:
                return value
        else:
            return value
        if not candidate.startswith(_ZSTD):
            return value
        value = candidate
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value)
        if value.startswith(_ZSTD):
            value = _decompress_limited(value)
    return value


def decode_text(compressed, message):
    return _decode_message_content(_bounded_blob(compressed), _bounded_blob(message))


def _xml(text):
    text = re.sub(r"^[^\s<>:]+:\r?\n", "", text, count=1).strip()
    if not text.startswith("<"):
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def _visible_text(local_type, compressed, message, sender_id, conversation_type):
    raw = decode_text(compressed, message)
    if conversation_type == "group" and sender_id:
        raw = re.sub(r"^" + re.escape(sender_id) + r":\r?\n", "", raw, count=1)
    if int(local_type or 0) & 0xFFFFFFFF == 1:
        return raw
    root = _xml(raw)
    if root is None:
        return raw
    app = root.find(".//appmsg") if root.tag != "appmsg" else root
    if app is not None:
        return "\n".join(filter(None, [app.findtext("title"), app.findtext("des")]))
    return "" if root.tag == "msg" else raw


def _mentions_all(value):
    root = _xml(decode_text(None, value))
    if root is None:
        return False
    users = root.findtext(".//atuserlist") or ""
    return "notify@all" in re.split(r"[,;\s]+", users)


def _kind(local_type, compressed, message):
    value = int(local_type or 0)
    base = value & 0xFFFFFFFF
    if base == 49:
        subtype = value >> 32
        if not subtype:
            root = _xml(decode_text(compressed, message))
            app = root.find(".//appmsg") if root is not None and root.tag != "appmsg" else root
            try:
                subtype = int(app.findtext("type") or 0) if app is not None else 0
            except ValueError:
                subtype = 0
        return {6: "file", 19: "forwarded", 5: "link", 57: "reply"}.get(subtype, "app")
    return {1: "text", 3: "image", 34: "voice", 43: "video", 47: "sticker", 48: "location", 10000: "system"}.get(base, "unknown")


def _attachments(row):
    kind = row["kind"]
    if kind not in {"file", "image", "video", "voice", "sticker"}:
        return []
    result = {"type": kind}
    root = _xml(decode_text(row["compress_content"], row["message_content"]))
    if root is not None:
        if kind == "file":
            app = root.find(".//appmsg") if root.tag != "appmsg" else root
            if app is not None:
                result["name"] = app.findtext("title") or ""
                result["extension"] = app.findtext("appattach/fileext") or ""
                size = app.findtext("appattach/totallen")
                if size and size.isdecimal():
                    result["size_bytes"] = int(size)
                md5 = app.findtext("appattach/filemd5") or app.findtext("md5")
                if md5:
                    result["md5"] = md5
        else:
            node = root.find({"image": ".//img", "video": ".//videomsg", "voice": ".//voicemsg", "sticker": ".//emoji"}[kind])
            if node is not None and node.get("md5"):
                result["md5"] = node.get("md5")
    return [result]


def _connect(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.create_function("decode_text", 2, decode_text, deterministic=True)
    connection.create_function("message_text", 5, _visible_text, deterministic=True)
    connection.create_function("mentions_all", 1, _mentions_all, deterministic=True)
    connection.create_function("message_kind", 3, _kind, deterministic=True)
    return connection


def _source(config):
    return config.get("source", config)


def scan(config, window_start, window_end, where_sql="1", where_params=None, *, context=None, conversations=None):
    """Select the native half-open timestamp window, then apply the SQL predicate."""
    source = _source(config)
    account = str(source["account_id"])
    folder = Path(source["decrypted_dir"])
    databases = sorted(p for p in folder.glob("message_*.db") if re.fullmatch(r"message_\d+\.db", p.name))
    if not databases:
        raise FileNotFoundError("No decrypted WeChat message_*.db databases found")
    params = dict(where_params or {})
    if any(k.startswith("_wc_") for k in params):
        raise ValueError("SQL parameter prefix _wc_ is reserved")

    def records():
        for database in databases:
            connection = _connect(database)
            try:
                connection.execute("BEGIN")
                names = dict(connection.execute("SELECT rowid, user_name FROM Name2Id"))
                conversation_names = {hashlib.md5(name.encode()).hexdigest(): name for name in names.values()}
                tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") if _TABLE.fullmatch(r[0])]
                if not tables:
                    raise ValueError(f"Unsupported WeChat schema in {database.name}: no Msg_* tables")
                for table in tables:
                    conversation = conversation_names.get(table[4:])
                    if not conversation:
                        raise ValueError(f"Cannot resolve WeChat conversation for {database.name}/{table}")
                    conversation_type = "group" if conversation.endswith("@chatroom") else "direct"
                    if conversations is not None and (conversation_type,conversation) not in conversations:
                        continue
                    time_scope = 'm.create_time >= :_wc_start AND m.create_time < :_wc_end'
                    if context is not None:
                        time_scope = f'''({time_scope}) OR m.local_id IN (
                            SELECT local_id FROM "{table}" WHERE create_time < :_wc_start
                            ORDER BY create_time DESC,local_id DESC LIMIT :_wc_before)
                            OR m.local_id IN (SELECT local_id FROM "{table}" WHERE create_time >= :_wc_end
                            ORDER BY create_time,local_id LIMIT :_wc_after)'''
                    query = f'''WITH candidates AS (
                        SELECT m.*, :_wc_conversation AS conversation_id,
                          :_wc_conversation_type AS conversation_type,
                          coalesce(n.user_name,'') AS sender_id, m.create_time AS sent_at,
                          message_text(m.local_type,m.compress_content,m.message_content,coalesce(n.user_name,''),:_wc_conversation_type) AS text,
                          mentions_all(m.source) AS mentions_all,
                          message_kind(m.local_type,m.compress_content,m.message_content) AS kind
                        FROM "{table}" m LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id
                        WHERE {time_scope}
                    ) SELECT * FROM candidates WHERE ({where_sql}) ORDER BY sent_at, local_id'''
                    bound = dict(params, _wc_start=int(window_start), _wc_end=int(window_end),
                                 _wc_conversation=conversation,
                                 _wc_conversation_type=conversation_type)
                    if context is not None:
                        bound.update(_wc_before=context[0],_wc_after=context[1])
                    for row in connection.execute(query, bound):
                        server_id = int(row["server_id"] or 0)
                        identity = f"server:{server_id}" if server_id else f"local:{database.name}:{table}:{row['local_id']}"
                        yield {"platform": "wechat", "account_id": account,
                               "conversation_id": conversation, "conversation_type": row["conversation_type"],
                               "message_id": identity, "sender_id": row["sender_id"],
                               "sent_at": int(row["sent_at"]), "kind": row["kind"], "text": row["text"],
                               "mentions_all": bool(row["mentions_all"]), "attachments": _attachments(row),
                               "source": {"database": database.name, "table": table,
                                          "local_id": row["local_id"], "local_type": row["local_type"]}}
            finally:
                connection.close()
    return records()


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
