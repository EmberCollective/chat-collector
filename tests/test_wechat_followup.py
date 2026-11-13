import hashlib
from contextlib import closing
import json
import sqlite3

import pytest
import zstandard

from collector.adapters import wechat


def test_wcdb_zero_preallocation_does_not_become_live_pages():
    from collector.vendor.wechat.decrypt import _trim_preallocated_wal_tail
    header = bytearray(4096)
    header[28:32] = (2).to_bytes(4, 'big')
    header[24:28] = header[92:96] = b'ABCD'
    data = bytes(header) + b'x'*4096 + bytes(3*4096)
    info = {'committed_frames': 2, 'database_pages': 5}
    assert len(_trim_preallocated_wal_tail(data, header, 4096, info)) == 8192
    assert info['database_pages'] == 2 and info['preallocated_tail_pages'] == 3
    assert _trim_preallocated_wal_tail(data[:-1]+b'x', header, 4096, dict(info)) == data[:-1]+b'x'
    header[92:96] = b'DCBA'
    assert _trim_preallocated_wal_tail(data, header, 4096, dict(info)) == data


def test_write_after_coherent_read_is_picked_up_next_refresh(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    (source / 'message').mkdir(parents=True)
    database = source / 'message/message_0.db'
    table = 'Msg_' + hashlib.md5(b'group').hexdigest()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(f'CREATE TABLE {table}(local_id INTEGER, create_time INTEGER)')
        connection.execute(f'INSERT INTO {table} VALUES(1,100)')
        connection.commit()
    key_file = tmp_path / 'keys.json'
    key_file.write_text(json.dumps({'test': {'db_key': '01' * 32}}))
    config = {'account_id': 'test', 'encrypted_dir': str(source),
              'decrypted_dir': str(tmp_path / 'snapshot'), 'key_file': str(key_file)}
    original = wechat.WeChatDatabaseDecryptor.decrypt_database
    calls = 0

    def simulate_wechat_write_after_read(self, source_path, target_path):
        nonlocal calls
        calls += 1
        success = original(self, source_path, target_path)
        assert success and not self.last_result['source_changed_during_read']
        if calls == 1:
            with closing(sqlite3.connect(source_path)) as connection:
                connection.execute(f'INSERT INTO {table} VALUES(2,200)')
                connection.commit()
        return success

    monkeypatch.setattr(wechat.WeChatDatabaseDecryptor, 'decrypt_database',
                        simulate_wechat_write_after_read)
    assert wechat.refresh(config)['updated'] == 1
    with closing(sqlite3.connect(tmp_path / 'snapshot/message_0.db')) as connection:
        assert connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 1
    assert wechat.refresh(config)['updated'] == 1
    with closing(sqlite3.connect(tmp_path / 'snapshot/message_0.db')) as connection:
        assert connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 2
    assert wechat.refresh(config)['updated'] == 0
    assert calls == 2


@pytest.mark.parametrize('representation', ['hex_blob', 'nested_zstd'])
def test_all_decode_paths_enforce_message_limit(representation):
    compressor = zstandard.ZstdCompressor()
    oversized = compressor.compress(b'A' * (17 * 1024 * 1024))
    encoded = oversized.hex().encode('ascii')
    payload = encoded if representation == 'hex_blob' else compressor.compress(encoded)
    with pytest.raises(ValueError, match='limit'):
        wechat.decode_text(None, payload)


def test_truncated_valid_frame_is_not_silently_decoded():
    payload = zstandard.ZstdCompressor().compress(b'complete message')
    with pytest.raises(ValueError):
        wechat.decode_text(None, payload[:-2])


def test_unknown_frame_size_still_enforces_limit():
    payload = zstandard.ZstdCompressor(write_content_size=False).compress(b'A' * (17 * 1024 * 1024))
    with pytest.raises(ValueError, match='limit'):
        wechat.decode_text(None, payload)


def test_time_indexes_rollback_together_when_one_table_is_invalid(tmp_path):
    database = tmp_path / 'snapshot.db'
    first = 'Msg_' + '1' * 32
    second = 'Msg_' + '2' * 32
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(f'CREATE TABLE {first}(local_id INTEGER, create_time INTEGER)')
        connection.execute(f'CREATE TABLE {second}(local_id INTEGER)')
        connection.commit()
    with pytest.raises(sqlite3.OperationalError, match='create_time'):
        wechat._index_snapshot(database)
    with closing(sqlite3.connect(database)) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name LIKE '_collector_time_%'").fetchall()


def test_hmac_diagnostics_are_bounded_without_skipping_pages(tmp_path, monkeypatch):
    sqlcipher = pytest.importorskip('sqlcipher3.dbapi2')
    from collector.vendor.wechat import decrypt
    source = tmp_path / 'encrypted.db'
    key = 'a1' * 32
    with closing(sqlcipher.connect(str(source))) as connection:
        connection.execute('PRAGMA key="x\'' + key + '\'"')
        connection.execute('CREATE TABLE proof(value TEXT)')
        connection.execute("INSERT INTO proof VALUES('preserved')")
        connection.commit()
    # WCDB preallocates pages beyond SQLite's logical size; retain their diagnostics counts.
    with source.open('ab') as stream:
        stream.write(bytes(12 * decrypt.PAGE_SIZE))
    calls = []
    build = decrypt._build_page_anomaly_debug
    def sample(*args, **kwargs):
        calls.append(args[3])
        return build(*args, **kwargs)
    monkeypatch.setattr(decrypt, '_build_page_anomaly_debug', sample)
    decoder = decrypt.WeChatDatabaseDecryptor(key)
    target = tmp_path / 'plain.db'
    assert decoder.decrypt_database(str(source), str(target))
    assert decoder.last_result['hmac_warning_pages'] == 12
    assert len(calls) == len(decoder.last_result['hmac_debug_samples']) == 8
    assert decoder.last_result['successful_pages'] == source.stat().st_size // decrypt.PAGE_SIZE
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute('SELECT value FROM proof').fetchall() == [('preserved',)]
