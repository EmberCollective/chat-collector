import hashlib
import json
import sqlite3

import pytest
import zstandard

from collector.adapters import wechat


@pytest.fixture
def messages(tmp_path):
    peer = "group1@chatroom"
    table = "Msg_" + hashlib.md5(peer.encode()).hexdigest()
    database = tmp_path / "message_0.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
    connection.executemany("INSERT INTO Name2Id VALUES(?)", [(peer,), ("sender1",)])
    connection.execute(f'''CREATE TABLE "{table}"(local_id INTEGER PRIMARY KEY,
        server_id INTEGER, local_type INTEGER, real_sender_id INTEGER, create_time INTEGER,
        source BLOB, message_content BLOB, compress_content BLOB)''')
    connection.execute(f'CREATE INDEX time_index ON "{table}"(create_time)')
    compressed = zstandard.ZstdCompressor().compress(b"sender1:\nplease review deadline")
    atall = zstandard.ZstdCompressor().compress(b"<msgsource><atuserlist>notify@all,sender2</atuserlist></msgsource>")
    file_xml = '<msg><appmsg><title>schedule.pdf</title><type>6</type><appattach><totallen>42</totallen><fileext>pdf</fileext></appattach></appmsg></msg>'
    rows = [(1, 101, 1, 2, 100, atall, compressed, None),
            (2, 102, (6 << 32) | 49, 2, 120, None, file_xml, None),
            (3, 103, 1, 2, 200, None, "@所有人 plain text is not a real mention", None)]
    connection.executemany(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?,?)', rows)
    connection.commit()
    connection.close()
    return {"account_id": "test", "decrypted_dir": str(tmp_path)}, database, table


def test_native_time_window_and_sql_filters(messages):
    config, _, _ = messages
    result = list(wechat.scan(config, 100, 200, "mentions_all=1 AND instr(text,:keyword)>0", {"keyword": "deadline"}))
    assert len(result) == 1
    assert result[0]["text"] == "please review deadline"
    assert result[0]["mentions_all"] is True
    assert result[0]["sender_id"] == "sender1"
    assert result[0]["message_id"] == "server:101"
    assert len(list(wechat.scan(config, 100, 200))) == 2
    assert len(list(wechat.scan(config, 200, 201))) == 1
    assert not list(wechat.scan(config, 200, 201))[0]["mentions_all"]


def test_file_metadata_and_native_columns(messages):
    config, _, _ = messages
    result = list(wechat.scan(config, 100, 200, "kind='file' AND local_type=:native_type", {"native_type": (6 << 32) | 49}))
    assert len(result) == 1
    assert result[0]["attachments"] == [{"type": "file", "name": "schedule.pdf", "extension": "pdf", "size_bytes": 42}]


def test_plain_text_is_not_mistaken_for_xml_or_atall():
    assert wechat.decode_text(None, "68656c6c6f") == "68656c6c6f"
    assert wechat.decode_text(None, "a===") == "a==="
    assert not wechat._mentions_all("notify@all")
    assert not wechat._mentions_all("@所有人")
    assert wechat._visible_text(1, None, "<msg>literal XML text</msg>", "sender1", "group") == "<msg>literal XML text</msg>"
    assert wechat._visible_text(1, None, "title:\nnotes", "sender1", "group") == "title:\nnotes"


def test_corrupt_compressed_payload_fails_explicitly():
    with pytest.raises(ValueError, match="Invalid or over-limit"):
        wechat.decode_text(None, b"\x28\xb5\x2f\xfdinvalid")


def test_sql_cannot_modify_database(messages):
    config, database, table = messages
    with pytest.raises(sqlite3.Error):
        list(wechat.scan(config, 100, 200, f'1); DROP TABLE "{table}"; --'))
    with sqlite3.connect(database) as connection:
        assert connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 3


def test_missing_schema_is_not_an_empty_success(tmp_path):
    with sqlite3.connect(tmp_path / "message_0.db") as connection:
        connection.execute("CREATE TABLE unrelated(x)")
    with pytest.raises(sqlite3.Error):
        list(wechat.scan({"account_id": "test", "decrypted_dir": str(tmp_path)}, 0, 100))


def test_refresh_reuses_unchanged_snapshot_and_keeps_source(messages, tmp_path):
    config, database, _ = messages
    encrypted = tmp_path / "source"
    (encrypted / "message").mkdir(parents=True)
    original = database.read_bytes()
    (encrypted / "message/message_0.db").write_bytes(original)
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"test": {"db_key": "01" * 32}}))
    refresh_config = {"account_id": "test", "encrypted_dir": str(encrypted),
                      "decrypted_dir": str(tmp_path / "decoded"), "key_file": str(keys)}
    refreshed = wechat.refresh(refresh_config)
    assert refreshed["updated"] == 1
    assert refreshed['timings_seconds']['total'] >= 0
    assert set(refreshed['validation'][0]['timings_seconds']) == {'decrypt_and_validate', 'time_indexes'}
    assert wechat.refresh(refresh_config)["updated"] == 0
    assert (encrypted / "message/message_0.db").read_bytes() == original
    assert len(list(wechat.scan(refresh_config, 100, 200))) == 2
    with sqlite3.connect(tmp_path / "decoded/message_0.db") as connection:
        table = messages[2]
        plan = list(connection.execute(f'EXPLAIN QUERY PLAN SELECT local_id FROM "{table}" WHERE create_time>=100 AND create_time<200'))
        assert any("SEARCH" in row[3] for row in plan)
    resource = encrypted / "message/message_resource.db"
    with sqlite3.connect(resource) as connection:
        connection.execute('CREATE TABLE resource_test(value)')
        connection.execute('INSERT INTO resource_test VALUES(1)')
    refresh_config['resource_db'] = str(tmp_path / 'decoded/message_resource.db')
    assert wechat.refresh(refresh_config)['updated'] == 1
    with sqlite3.connect(resource) as connection:
        connection.execute('INSERT INTO resource_test VALUES(2)')
    assert wechat.refresh(refresh_config)['updated'] == 1
    with sqlite3.connect(refresh_config['resource_db']) as connection:
        assert connection.execute('SELECT count(*) FROM resource_test').fetchone()[0] == 2
