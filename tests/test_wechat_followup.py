import hashlib
from contextlib import closing
import json
import sqlite3

import pytest
import zstandard

from collector.adapters import wechat


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
