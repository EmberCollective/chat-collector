"""Recovery uses the collector snapshot boundary, without scanning real processes."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from collector.adapters import qq


@pytest.fixture
def runner(tmp_path, monkeypatch):
    from collector import config
    settings = {'source': {'encrypted_dir': str(tmp_path), 'account_id': 'test-account',
                           'key_file': str(tmp_path / 'key.json')}}
    monkeypatch.setattr(config, 'load_config', lambda *args: settings)
    path = Path(__file__).resolve().parents[1] / 'tools/qq_recover.py'
    spec = importlib.util.spec_from_file_location('qq_recovery_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Keep even the upstream scanner's empty config in the isolated test directory.
    module.__file__ = str(tmp_path / 'tools/qq_recover.py')
    (tmp_path / 'tools/qq-nt-export').mkdir(parents=True)
    module.SOURCE.write_bytes(bytes(1024) + b'encrypted-pages')
    Path(str(module.SOURCE) + '-wal').write_bytes(b'captured-prefix')
    def scan(args, **kwargs):
        Path(args[args.index('--json-out') + 1]).write_text(json.dumps({
            'scan_stats': [], 'confirmed': [{'key_hex': '11' * 32, 'salt_hex': '22' * 16, 'salt_match': True}]}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', scan)
    return module


def test_recovery_accepts_concurrent_wal_append_then_authenticates(runner, monkeypatch):
    import sqlcipher3.dbapi2 as sqlcipher
    real = qq._prefix_digest
    def appending(stream, size, destination=None):
        result = real(stream, size, destination)
        if destination is not None:
            with Path(str(runner.SOURCE) + '-wal').open('ab') as wal:
                wal.write(b'new-transaction')
        return result
    monkeypatch.setattr(qq, '_prefix_digest', appending)
    queries = []
    class Connection:
        def execute(self, sql):
            queries.append(sql)
            return [('group_msg_table',)] if 'sqlite_master' in sql else self
        def fetchone(self): return (1,)
        def close(self): pass
    def connect(path, **kwargs):
        assert Path(path).read_bytes() == b'encrypted-pages'
        assert Path(path + '-wal').read_bytes() == b'captured-prefix'
        return Connection()
    monkeypatch.setattr(sqlcipher, 'connect', connect)
    assert runner.main() == 0
    assert any('SELECT 1 FROM' in query for query in queries)
    assert json.loads(runner.KEY_FILE.read_text())['account'] == 'test-account'
    assert not list(runner.PRIVATE.glob('verify-*'))


def test_recovery_rejects_rewritten_wal_without_replacing_key(runner, monkeypatch):
    import sqlcipher3.dbapi2 as sqlcipher
    runner.KEY_FILE.write_text('existing-key-preserved')
    real = qq._prefix_digest
    def rewriting(stream, size, destination=None):
        result = real(stream, size, destination)
        if destination is not None:
            wal = Path(str(runner.SOURCE) + '-wal')
            data = wal.read_bytes()
            wal.write_bytes((b'x' if data[:1] != b'x' else b'y') + data[1:])
        return result
    monkeypatch.setattr(qq, '_prefix_digest', rewriting)
    monkeypatch.setattr(sqlcipher, 'connect', lambda *a, **k: pytest.fail('Unstable snapshot must not be authenticated'))
    assert runner.main() == 2
    assert runner.KEY_FILE.read_text() == 'existing-key-preserved'
    assert not list(runner.PRIVATE.glob('verify-*'))
