"""A partly committed WeChat refresh cannot become a readable source generation."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from collector.adapters import wechat
from collector.service import collect_once, publication_path, source_snapshot
import selection
from test_filters import config as source_fixture, register


@pytest.fixture
def source(tmp_path, monkeypatch):
    cfg = source_fixture.__wrapped__(SimpleNamespace(param='wechat'), tmp_path)
    encrypted = tmp_path/'encrypted'/'message'
    encrypted.mkdir(parents=True)
    for path in Path(cfg['source']['decrypted_dir']).glob('*.db'):
        shutil.copy2(path, encrypted/path.name)
    key = tmp_path/'key.json'
    key.write_text(json.dumps({'account': {'db_key': '00'*32}}), encoding='utf-8')
    cfg['source'].update(refresh=True, encrypted_dir=str(encrypted.parent), key_file=str(key))
    failures = set()

    class Decryptor:
        def __init__(self, key):
            self.last_result = {'diagnostics': {'quick_check_ok': True}, 'error': 'fixture failure'}

        def decrypt_database(self, original, target):
            if Path(original).name in failures:
                return False
            shutil.copy2(original, target)
            return True

    monkeypatch.setattr(wechat, 'WeChatDatabaseDecryptor', Decryptor)
    register(cfg, 'all.sql')
    return cfg, failures


def change_first_shard(cfg):
    path = Path(cfg['source']['encrypted_dir'])/'message'/'message_0.db'
    with closing(sqlite3.connect(path)) as db, db:
        table = db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'Msg_%' ORDER BY name").fetchone()[0]
        db.execute(f'UPDATE "{table}" SET message_content=?', ('new generation',))
    with closing(sqlite3.connect(path.with_name('message_1.db'))) as db:
        db.execute('PRAGMA user_version=7')


@pytest.mark.parametrize('published_before', [False, True])
def test_partial_refresh_and_restart_reject_replay_until_collection_completes(source, published_before):
    cfg, failures = source
    if published_before:
        old = collect_once(cfg)
    change_first_shard(cfg)
    failures.add('message_1.db')
    with pytest.raises(RuntimeError, match='message_1.db'):
        collect_once(cfg)
    if published_before:
        assert json.loads(publication_path(cfg).read_text(encoding='utf-8')) == old
    # Every individual public DB is still valid; the set is not a published generation.
    for path in Path(cfg['source']['decrypted_dir']).glob('*.db'):
        with closing(sqlite3.connect(path)) as db:
            assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
    with pytest.raises(RuntimeError, match='complete collection publication'):
        selection.run_round(cfg, 100, 111)
    with pytest.raises(RuntimeError, match='complete collection publication'):
        with source_snapshot(cfg):
            pytest.fail('Unpublished source reached an attachment/query reader')
    cfg['source']['refresh'] = False
    with pytest.raises(RuntimeError, match='incomplete'):
        collect_once(cfg)
    cfg['source']['refresh'] = True
    failures.clear()
    result = collect_once(cfg)
    assert result['refresh']['updated'] == 2
    assert selection.run_round(cfg, 100, 111)['emitted'] > 0
    assert not publication_path(cfg).with_suffix('.pending.json').exists()


def test_unchanged_failed_refresh_keeps_the_published_view_readable(source):
    cfg, _ = source
    old = collect_once(cfg)
    # Refresh fails before any source changes. The original published view stays usable.
    Path(cfg['source']['key_file']).unlink()
    with pytest.raises(FileNotFoundError):
        collect_once(cfg)
    with source_snapshot(cfg) as snapshot:
        assert snapshot['snapshot_id'] == old['snapshot_id']
    assert selection.run_round(cfg, 100, 111)['emitted'] > 0
