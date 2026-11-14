"""Map native QQ rows to the standard query message contract."""
import base64
import json
from pathlib import Path
import sqlite3

from collector.adapters.qq_vendor.c2c_40800_parser import parse_40800


def _body(blob):
    parsed = parse_40800(blob)
    attachments = []
    types = {item.content_type for item in parsed.contents}
    for item in parsed.contents:
        if item.content_type in (2, 3, 4, 5):
            attachments.append({
                "type": {2: "image", 3: "file", 4: "audio", 5: "video"}[item.content_type],
                "name": item.filename or None,
                "local_path": (item.filepath or item.local_path).removeprefix("::NTOSFull::") or None,
                "size_bytes": item.filesize or None,
                "md5": item.md5_raw.hex() or None,
            })
    kind = next((name for code, name in ((3, "file"), (2, "image"), (4, "audio"), (5, "video")) if code in types), "text" if 1 in types else "other")
    return json.dumps({
        "text": "\n".join(item.text for item in parsed.contents if item.text),
        "kind": kind,
        "attachments": attachments,
        "parse_status": parsed.status.value,
        # Field 45102: 0 plain text, 1 all members, 2 an individual mention.
        "mentions_all": any(item.content_type == 1 and item.text_attr == 1 for item in parsed.contents),
        "parse_warnings": [] if parsed.status.value == "typed" else ["qq_body_not_fully_parsed"],
    }, ensure_ascii=False)


def scan(config, window_start, window_end, where_sql="1", where_params=None, *, context=None, conversations=None):
    """Yield the half-open time window; predicates are trusted local configuration."""
    if window_end < window_start or (window_end == window_start and context is None):
        raise ValueError("window_end must be greater than window_start")
    bindings = dict(where_params or {})
    if any(key.startswith("_qq_") for key in bindings):
        raise ValueError("SQL parameter names beginning with _qq_ are reserved")
    bindings.update(_qq_start=window_start, _qq_end=window_end)
    path = Path(config.get("database") or Path(config["decrypted_dir"]) / "nt_msg.db").resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.create_function("qq_body", 1, _body, deterministic=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        available = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = [(name, kind) for name, kind in (("group_msg_table", "group"), ("c2c_msg_table", "direct")) if name in available]
        if not tables:
            raise ValueError("NTQQ message tables are missing; the snapshot schema is unsupported")
        for table, conversation_type in tables:
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            required = {"40001", "40050", "40030", "40021", "40033", "40020", "40011", "40800"}
            if not required <= columns:
                raise ValueError(f"Unsupported {table} schema; missing {sorted(required-columns)}")
            conversation_sql = 'COALESCE(CAST(NULLIF("40030",0) AS TEXT),"40021")'
            time_scope = '"40050">=:_qq_start AND "40050"<:_qq_end'
            if conversations is not None:
                peers = sorted(peer for kind,peer in conversations if kind==conversation_type)
                if not peers:
                    continue
                bindings['_qq_peers'] = json.dumps(peers)
                if context is not None:
                    ids = set()
                    for peer in peers:
                        for count,operator,boundary,order in ((context[0],'<',window_start,'DESC'),(context[1],'>=',window_end,'ASC')):
                            if count:
                                ids.update(row[0] for row in conn.execute(f'''SELECT "40001" FROM "{table}"
                                    WHERE {conversation_sql}=? AND "40050" {operator} ?
                                    ORDER BY "40050" {order},"40001" {order} LIMIT ?''',(peer,boundary,count)))
                    bindings['_qq_neighbors'] = json.dumps(sorted(ids))
                    time_scope = f'({time_scope}) OR "40001" IN (SELECT value FROM json_each(:_qq_neighbors))'
                time_scope = f'({time_scope}) AND {conversation_sql} IN (SELECT value FROM json_each(:_qq_peers))'
            sql = f'''WITH raw AS MATERIALIZED (
                SELECT CAST("40001" AS TEXT) AS message_id,
                    COALESCE(CAST(NULLIF("40030",0) AS TEXT),"40021") AS conversation_id,
                    :_qq_type AS conversation_type,
                    COALESCE(CAST(NULLIF("40033",0) AS TEXT),"40020") AS sender_id,
                    "40050" AS sent_at, "40011" AS raw_type, "40800" AS raw_body,
                    qq_body("40800") AS payload
                FROM "{table}" WHERE {time_scope}
            ), normalized AS (
                SELECT *, json_extract(payload,'$.text') AS text,
                    json_extract(payload,'$.kind') AS kind,
                    json_extract(payload,'$.mentions_all') AS mentions_all
                FROM raw
            ) SELECT * FROM normalized WHERE ({where_sql}) ORDER BY sent_at,message_id'''
            bindings["_qq_type"] = conversation_type
            for row in conn.execute(sql, bindings):
                record = {name: row[name] for name in ("message_id", "conversation_id", "conversation_type", "sender_id", "sent_at", "text", "kind")}
                body = json.loads(row["payload"])
                metadata = {"parse_status": body["parse_status"], "parse_warnings": body["parse_warnings"]}
                if body["parse_status"] != "typed" or body["kind"] == "other":
                    metadata["raw_body_base64"] = base64.b64encode(row["raw_body"] or b"").decode("ascii")
                record.update(platform="qq", account_id=str(config["account_id"]),
                    source={"database": str(path), "table": table, "local_id": row["message_id"], "raw_type": row["raw_type"]},
                    mentions_all=body["mentions_all"], attachments=body["attachments"],
                    metadata=metadata)
                yield record
    finally:
        conn.close()
