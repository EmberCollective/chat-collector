"""Installation checks use synthetic inputs and never call clients or a model."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tomllib
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location('install_' + name, ROOT / 'tools/install' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def install_root(tmp_path):
    (tmp_path / 'config').mkdir()
    for p in (ROOT / 'tools/install/defaults').glob('*.toml'):
        shutil.copyfile(p, tmp_path / 'config' / p.name)
    return tmp_path


def test_defaults_valid_offline_and_private_data_absent(install_root):
    from collector.config import load_config as load_collector
    from processor import load_config as load_processor
    from automation import load_config as load_auto
    from messaging import load_config as load_messaging
    for name in ('wechat', 'qq'):
        config = load_collector(install_root / 'config' / (name + '.toml'))
        assert config['schedule']['initial_lookback_seconds'] == 604800
        assert config['source']['account_id'] == 'unconfigured'
    processing = load_processor(install_root / 'config/processor.toml')
    assert processing['agent']['auto_compaction'] is True
    assert processing['model']['api_key_file'] == ''
    auto = load_auto(install_root / 'config/automation.toml')
    assert not auto['enabled'] and not any(j['enabled'] for j in auto['jobs'].values())
    messaging = load_messaging(install_root / 'config/messaging.toml')
    assert not messaging['enabled'] and not any(t['enabled'] for t in messaging['targets'])
    text = '\n'.join(p.read_text(encoding='utf-8') for p in (install_root / 'config').glob('*'))
    assert not any(s in text for s in ('Administrator', 'wxid_jxfwv4ublsp322', '997698095', 'oc_56ba', 'ou_b04'))


def test_first_run_drains_batches_and_resumes_without_recollecting(install_root, monkeypatch):
    setup = module('setup')
    from collector import __main__ as collector
    import processor, messaging
    monkeypatch.setattr(setup, 'recover_key', Mock())
    collect = Mock(return_value={'status': 'succeeded'})
    process = Mock(side_effect=[{'pending_groups': 2, 'processed_groups': 10}, {'pending_groups': 0, 'processed_groups': 2}])
    sync = Mock()
    monkeypatch.setattr(collector, 'run_round', collect)
    monkeypatch.setattr(processor, 'run_round', process)
    monkeypatch.setattr(messaging, 'sync', sync)
    state = {'platforms': ['wechat', 'qq'], 'start_at': int(setup.time.time()) - 604800}
    snapshots = []
    setup.first_run(install_root, state, lambda: snapshots.append(dict(state)))
    assert collect.call_count == 2 and process.call_count == 2
    for call in collect.call_args_list:
        assert call.kwargs == {'start': state['start_at'], 'end': state['start_at'] + 604800}
    assert state['processed'] and state['collected_wechat'] and state['collected_qq']
    setup.first_run(install_root, state, lambda: None)
    assert collect.call_count == 2 and process.call_count == 2
    assert all(x['start_at'] == state['start_at'] for x in snapshots)


def test_failed_collection_retains_completed_platform(install_root, monkeypatch):
    setup = module('setup')
    from collector import __main__ as collector
    import processor
    monkeypatch.setattr(setup, 'recover_key', Mock())
    monkeypatch.setattr(collector, 'run_round', Mock(side_effect=[{}, RuntimeError('synthetic_failure')]))
    monkeypatch.setattr(processor, 'run_round', Mock(side_effect=AssertionError('must not run model')))
    state = {'platforms': ['wechat', 'qq'], 'start_at': 100}
    with pytest.raises(RuntimeError, match='synthetic_failure'):
        setup.first_run(install_root, state, lambda: None)
    assert state['collected_wechat'] and not state.get('collected_qq')
    assert not state.get('processed')


def test_existing_install_never_overwrites_config(install_root, monkeypatch):
    setup = module('setup')
    original = (install_root / 'config/processor.toml').read_bytes()
    with pytest.raises(RuntimeError, match='已有部署'):
        setup.wizard(install_root)
    assert (install_root / 'config/processor.toml').read_bytes() == original


def test_launcher_requires_ready_before_starting_workers(tmp_path):
    launch = module('launcher')
    with pytest.raises(RuntimeError, match='Configure.cmd'):
        launch.start_all(tmp_path)


def test_release_payload_never_copies_accounts_keys_or_installed_node():
    build = module('build_release')
    files = build.payload(ROOT)
    assert 'tools/install/defaults/processor.toml' in files
    assert 'tools/install/console.toml' in files
    assert 'tools/console/console.toml' not in files
    assert 'runtime-deps/native/sqlcipher-pages.dll' in files
    assert 'runtime-deps/sqlcipher3/_sqlite3.cp313-win_amd64.pyd' in files
    assert not any(p.startswith(('data/', 'secrets/', 'config/', '.git/', '.runtime/')) for p in files)
    assert not any('/node_modules/' in p or '/__pycache__/' in p for p in files)
    assert '.env' not in files
    assert 'direct_url.json' not in '\n'.join(files)


def test_registration_defaults_use_official_sdk_and_callback_scope():
    # Verify the actual call's literal addons against the SDK's own schema.
    import ast
    from lark_oapi.scene.registration import _normalize_addons
    tree = ast.parse((ROOT / 'tools/install/setup.py').read_text(encoding='utf-8'))
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'aregister_app')
    addons = ast.literal_eval(next(k.value for k in call.keywords if k.arg == 'addons'))
    assert _normalize_addons(addons)['callbacks']['items'] == ['card.action.trigger']
    assert 'im:resource' in addons['scopes']['tenant']
    assert 'im:resource:upload' not in addons['scopes']['tenant']


def test_startup_failure_does_not_report_success(tmp_path, monkeypatch):
    from tools.install import startup
    with pytest.raises(RuntimeError, match='尚未安装'):
        startup.set_startup(tmp_path, True)
    with pytest.raises(ValueError):
        startup.set_startup(tmp_path, 'true')


def test_no_progress_in_first_processing_is_not_marked_complete(install_root, monkeypatch):
    setup = module('setup')
    import processor
    monkeypatch.setattr(processor, 'run_round', lambda config: {'pending_groups': 2, 'processed_groups': 0})
    state = {'platforms': [], 'start_at': 123}
    with pytest.raises(RuntimeError, match='没有推进'):
        setup.first_run(install_root, state, lambda: None)
    assert not state.get('processed')


def test_invalid_model_settings_do_not_replace_saved_configuration(install_root):
    setup = module('setup')
    path = install_root / 'config/processor.toml'
    before = path.read_bytes()
    settings = setup.read_config(install_root, 'processor')
    settings['model']['context_window'] = 100
    with pytest.raises(ValueError):
        setup.save_config(install_root, 'processor', settings)
    assert path.read_bytes() == before
    assert not path.with_suffix('.tmp').exists()


def test_console_port_choice_rejects_busy_port_without_changing_config(tmp_path, monkeypatch):
    import socket
    setup = module('setup')
    path = tmp_path / 'tools/console/console.toml'
    path.parent.mkdir(parents=True)
    path.write_text('port = 10400\n', encoding='utf-8')
    original = path.read_bytes()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        busy = sock.getsockname()[1]
        monkeypatch.setattr(setup, 'ask', lambda *args: str(busy))
        with pytest.raises(RuntimeError, match='已被占用'):
            setup.configure_console(tmp_path)
    assert path.read_bytes() == original
    monkeypatch.setattr(setup, 'ask', lambda *args: str(busy))
    setup.configure_console(tmp_path)
    assert tomllib.loads(path.read_text())['port'] == busy


def test_wechat_hook_waits_and_retries_main_process(monkeypatch):
    from types import SimpleNamespace
    import psutil
    recovery = module('recover_wechat')
    now = [0.0]
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(recovery.time, 'sleep', lambda delay: now.__setitem__(0, now[0] + delay))
    exe = 'C:/Weixin/Weixin.exe'
    child = SimpleNamespace(pid=2, info={'exe': exe, 'cmdline': [exe, '--type=renderer']})
    main = SimpleNamespace(pid=1, info={'exe': exe, 'cmdline': [exe]})
    monkeypatch.setattr(psutil, 'process_iter', lambda fields: [child, main])
    native = Mock()
    native.initialize_hook.side_effect = [False, False, True]
    native.get_last_error_msg.return_value = 'module not loaded'
    assert recovery.connect_hook(exe, native) == 1
    assert now[0] >= 3
    assert all(call.args == (1,) for call in native.initialize_hook.call_args_list)
    assert native.cleanup_hook.call_count == 2


def test_wechat_hook_timeout_preserves_native_diagnostic(monkeypatch):
    from types import SimpleNamespace
    import psutil
    recovery = module('recover_wechat')
    now = [0.0]
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(recovery.time, 'sleep', lambda delay: now.__setitem__(0, now[0] + delay))
    exe = 'C:/Weixin/Weixin.exe'
    monkeypatch.setattr(psutil, 'process_iter', lambda fields: [SimpleNamespace(pid=1, info={'exe': exe, 'cmdline': [exe]})])
    native = Mock()
    native.initialize_hook.return_value = False
    native.get_last_error_msg.return_value = 'module not loaded'
    with pytest.raises(RuntimeError, match='module not loaded'):
        recovery.connect_hook(exe, native, timeout=3)
    assert native.initialize_hook.call_count == native.cleanup_hook.call_count == 2
