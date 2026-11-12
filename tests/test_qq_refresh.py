"""Exercise official SQLCipher export, including live WAL and failure atomicity."""
import errno
import json
from pathlib import Path
import sqlite3

import pytest

sqlcipher = pytest.importorskip("sqlcipher3.dbapi2")

from collector.adapters.qq import refresh, _publish_snapshot


@pytest.mark.parametrize("mode", ["passphrase", "raw_key_salt"])
def test_export_includes_wal_and_keeps_last_good_snapshot(tmp_path, mode, monkeypatch):
    work = tmp_path / 'ssd'
    replace = Path.replace
    def cross_volume(path, target):
        if work in path.parents and work not in target.parents:
            raise OSError(errno.EXDEV, 'different volume')
        return replace(path, target)
    monkeypatch.setattr(Path, 'replace', cross_volume)
    encrypted = tmp_path / "source"
    encrypted.mkdir()
    db = encrypted / "fixture.db"
    # Synthetic credentials only; never taken from an actual account.
    secret = "fixture-password" if mode == "passphrase" else "a1" * 32 + "b2" * 16
    live = sqlcipher.connect(str(db), isolation_level=None)
    try:
        key_sql = "'fixture-password'" if mode == "passphrase" else '"x\'' + secret + '\'"'
        live.execute("PRAGMA cipher_page_size=4096")
        live.execute("PRAGMA key=" + key_sql)
        live.execute("PRAGMA kdf_iter=4000")
        live.execute("PRAGMA cipher_hmac_algorithm=HMAC_SHA1")
        live.execute("PRAGMA cipher_kdf_algorithm=PBKDF2_HMAC_SHA512")
        live.execute("CREATE TABLE proof(value TEXT)")
        live.execute("INSERT INTO proof VALUES ('base')")
        live.execute("INSERT INTO proof(rowid,value) VALUES (42,'gap')")
        live.execute('CREATE INDEX proof_value ON proof(value)')
        live.execute('CREATE TABLE sequence_proof(id INTEGER PRIMARY KEY AUTOINCREMENT)')
        live.execute('INSERT INTO sequence_proof VALUES(100)')
        live.execute('DELETE FROM sequence_proof')
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("INSERT INTO proof VALUES ('wal')")
        source = encrypted / "nt_msg.db"
        source.write_bytes(bytes(1024) + db.read_bytes())
        (encrypted / "nt_msg.db-wal").write_bytes(db.with_name(db.name + "-wal").read_bytes())
        key_file = tmp_path / "key.json"
        key_file.write_text(json.dumps({"account": "fixture", "key": secret, "key_mode": mode}), encoding="utf-8")
        config = {"account_id": "fixture", "encrypted_dir": str(encrypted),
                  "decrypted_dir": str(tmp_path / "plain"), "key_file": str(key_file),
                  "work_dir": str(work)}
        result = refresh(config)
        assert result["changed"] is True
        assert result['tables']['proof'] == 3
        assert result['tables']['sequence_proof'] == 0
        assert set(result['timings_seconds']) == {'capture','export','integrity_check','time_indexes','publish','total'}
        with sqlite3.connect(result["database"]) as plain:
            assert plain.execute("SELECT rowid,value FROM proof ORDER BY rowid").fetchall() == [(1,"base"), (42,"gap"), (43,"wal")]
            assert plain.execute("SELECT seq FROM sqlite_sequence WHERE name='sequence_proof'").fetchone() == (100,)
            assert plain.execute("SELECT name FROM sqlite_master WHERE name='proof_value'").fetchone()
        assert refresh(config)["changed"] is False
        assert list(work.iterdir()) == []
        original = (tmp_path / "plain" / "nt_msg.db").read_bytes()
        key_file.write_text(json.dumps({"account": "fixture", "key": "wrong"}), encoding="utf-8")
        # A changed source forces an attempted refresh with the wrong key.
        import os
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
        with pytest.raises(sqlcipher.DatabaseError):
            refresh(config)
        assert (tmp_path / "plain" / "nt_msg.db").read_bytes() == original
        assert list(work.iterdir()) == []
    finally:
        live.close()


@pytest.mark.parametrize('failure', [None, 'copy', 'hash', 'permission'])
def test_cross_volume_publish_preserves_last_good_snapshot(tmp_path, monkeypatch, failure):
    from collector.adapters import qq
    plain, destination = tmp_path / 'plain.db', tmp_path / 'published.db'
    plain.write_bytes(b'new verified snapshot')
    destination.write_bytes(b'previous snapshot')
    replace = Path.replace
    def cross_volume(path, target):
        if path == plain:
            raise OSError(errno.EACCES if failure == 'permission' else errno.EXDEV, 'fixture')
        return replace(path, target)
    monkeypatch.setattr(Path, 'replace', cross_volume)
    if failure == 'copy':
        def broken_copy(*args):
            raise OSError('disk full')
        monkeypatch.setattr(qq, '_prefix_digest', broken_copy)
    elif failure == 'hash':
        monkeypatch.setattr(qq.hashlib, 'file_digest', lambda *args: qq.hashlib.sha256(b'corrupt'))
    if failure:
        with pytest.raises((OSError, RuntimeError)):
            _publish_snapshot(plain, destination)
        assert destination.read_bytes() == b'previous snapshot'
    else:
        _publish_snapshot(plain, destination)
        assert destination.read_bytes() == plain.read_bytes()
    assert not list(tmp_path.glob('.qq-publish-*'))


def test_same_volume_publish(tmp_path):
    plain, destination = tmp_path / 'plain.db', tmp_path / 'published.db'
    plain.write_bytes(b'new snapshot')
    destination.write_bytes(b'previous snapshot')
    _publish_snapshot(plain, destination)
    assert destination.read_bytes() == b'new snapshot'
    assert not plain.exists()
