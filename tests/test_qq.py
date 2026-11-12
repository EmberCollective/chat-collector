import sqlite3

import pytest

from collector.adapters.qq import scan
from collector.adapters.qq_vendor.c2c_40800_pb2 import MsgBody


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "nt_msg.db"
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE group_msg_table (
        "40001" INTEGER PRIMARY KEY,"40050" INTEGER,"40030" INTEGER,
        "40021" TEXT,"40033" INTEGER,"40020" TEXT,"40011" INTEGER,"40800" BLOB)''')
    for identifier, timestamp, text, kind in [(1, 100, "deadline", 1), (2, 150, "meeting", 3), (3, 200, "outside", 1)]:
        body = MsgBody()
        item = body.content.add(content_type=kind, text=text)
        if kind == 3:
            item.filename = "agenda.pdf"
            item.filesize = 321
        conn.execute('INSERT INTO group_msg_table VALUES (?,?,?,?,?,?,?,?)', (identifier,timestamp,123,"123",456,"u_sender",kind,body.SerializeToString()))
    conn.commit()
    conn.close()
    return {"account_id": "owner", "database": str(path)}


def test_time_window_and_payload(config):
    rows = list(scan(config, 100, 200))
    assert [r["message_id"] for r in rows] == ["1", "2"]
    assert rows[1]["kind"] == "file"
    assert rows[1]["attachments"][0]["name"] == "agenda.pdf"
    assert rows[0]["conversation_id"] == "123"
    assert rows[0]["mentions_all"] is False
    assert rows[0]["metadata"]["parse_warnings"] == []
    assert rows[0]["platform"] == "qq"
    assert rows[0]["source"]["table"] == "group_msg_table"


def test_sql_filter_uses_normalized_columns(config):
    rows = list(scan(config, 0, 300, "kind=:kind AND conversation_id=:group AND text LIKE :keyword", {"kind":"file", "group":"123", "keyword":"%meet%"}))
    assert [r["message_id"] for r in rows] == ["2"]


def test_invalid_protocol_is_retained(config):
    conn = sqlite3.connect(config["database"])
    conn.execute('UPDATE group_msg_table SET "40800"=? WHERE "40001"=1', (b"\xff",))
    conn.commit()
    conn.close()
    row = next(scan(config, 100, 101))
    assert row["metadata"]["parse_status"] == "invalid"
    assert row["metadata"]["raw_body_base64"] == "/w=="


def test_invalid_window(config):
    with pytest.raises(ValueError):
        list(scan(config, 100, 100))


def test_reserved_bindings_are_rejected(config):
    with pytest.raises(ValueError):
        list(scan(config, 100, 200, where_params={"_qq_start": 0}))


def test_mentions_filter_uses_metadata_not_display_text(config):
    conn = sqlite3.connect(config["database"])
    for identifier, attr in ((1, 0), (2, 1), (3, 2)):
        body = MsgBody()
        body.content.add(content_type=1, text="@全体成员", text_attr=attr)
        conn.execute('UPDATE group_msg_table SET "40800"=? WHERE "40001"=?', (body.SerializeToString(),identifier))
    conn.commit()
    conn.close()
    assert [row['message_id'] for row in scan(config, 0, 300, 'mentions_all = 1')] == ['2']


def test_context_never_crosses_group_and_direct_with_same_numeric_id(config):
    from collector.filters import scan_filtered
    body = MsgBody()
    body.content.add(content_type=1,text='private conversation')
    with sqlite3.connect(config['database']) as db:
        db.execute('CREATE TABLE c2c_msg_table AS SELECT * FROM group_msg_table WHERE 0')
        db.execute('INSERT INTO c2c_msg_table VALUES(99,150,123,"123",456,"sender",1,?)',(body.SerializeToString(),))
    settings = {'platform':'qq','source':config,'filters':{'rules':[
        {'type':'nearby','before':10,'after':0,'condition':{'type':'keywords','values':['deadline']}}]}}
    assert [row['message_id'] for row in scan_filtered(settings,0,300)] == ['1','2','3']
