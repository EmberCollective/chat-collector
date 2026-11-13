"""Resumable, local first-run wizard; existing core modules perform all real work."""
from pathlib import Path
import argparse
import asyncio
import ctypes
import getpass
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import webbrowser

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime-deps')]


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def read_config(root, name):
    import tomlkit
    return tomlkit.loads((root / 'config' / (name + '.toml')).read_text(encoding='utf-8'))


def save_config(root, name, doc):
    import importlib
    import tomlkit
    path = root / 'config' / (name + '.toml')
    tmp = path.with_suffix('.tmp')
    tmp.write_text(tomlkit.dumps(doc), encoding='utf-8')
    try:
        module = {'wechat': 'collector.config', 'qq': 'collector.config',
                  'processor': 'processor', 'messaging': 'messaging', 'automation': 'automation'}[name]
        importlib.import_module(module).load_config(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def ask(text, default=''):
    return input(text + (f' [{default}]' if default else '') + '：').strip().strip('"') or default


def yes(text, default=False):
    return ask(text + ' (y/n)', 'y' if default else 'n').lower() == 'y'


def choose(items, title):
    if not items:
        raise RuntimeError(title + '：没有可用选项，请确认账号登录、机器人已加入群及权限已发布。')
    print(title)
    for index, item in enumerate(items, 1):
        print(f'  {index}. {item[0]}')
    choice = int(ask('选择编号', '1' if len(items) == 1 else ''))
    if not 1 <= choice <= len(items):
        raise ValueError('选择编号无效')
    return items[choice - 1][1]


def discover_accounts(platform):
    import winreg
    roots = [Path.home() / 'Documents']
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
            roots.append(Path(os.path.expandvars(winreg.QueryValueEx(key, 'Personal')[0])))
    except OSError:
        pass
    matches = set()
    for root in roots:
        pattern = 'xwechat_files/*/db_storage' if platform == 'wechat' else 'Tencent Files/*/nt_qq/nt_db'
        matches.update(p.resolve() for p in root.glob(pattern) if p.is_dir())
    return sorted(matches)


def secure_secrets(root):
    if os.name != 'nt':
        return
    folder = root / 'secrets'
    folder.mkdir(exist_ok=True)
    sid = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'],
        creationflags=subprocess.CREATE_NO_WINDOW).decode('utf-8', errors='replace').strip().split(',')[-1].strip('"')
    if not sid.startswith('S-1-'):
        raise RuntimeError('无法确认当前 Windows 用户，未保存凭据。')
    for path in (folder, root / '.env'):
        if path.exists():
            subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r',
                f'*{sid}:' + ('(OI)(CI)F' if path.is_dir() else 'F'),
                '*S-1-5-18:' + ('(OI)(CI)F' if path.is_dir() else 'F')],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)


def secret(root, name, value):
    from dotenv import set_key
    if not value or '\n' in value or '\r' in value:
        raise ValueError('凭据不能为空或包含换行')
    path = root / '.env'
    path.touch(exist_ok=True)
    secure_secrets(root)
    set_key(str(path), name, value, quote_mode='always')
    secure_secrets(root)


def check_runtime():
    import cryptography, zstandard, google.protobuf, PIL, Crypto, pymupdf, docx
    import psutil, dotenv, httpx, notion_client, lark_oapi, tomlkit, apscheduler, fastapi, uvicorn
    import sqlcipher3.dbapi2 as sqlcipher
    import wx_key
    from collector.native_sqlite import NativeSQLite
    from processor.pi_bridge import runtime_status
    from zoneinfo import ZoneInfo
    ZoneInfo('Asia/Shanghai')
    with sqlcipher.connect(':memory:') as db:
        if not db.execute('PRAGMA cipher_version').fetchone():
            raise RuntimeError('SQLCipher is missing')
    for name in ('sqlcipher-pages.dll', 'sqlcipher-qq-pages.dll'):
        db = NativeSQLite(':memory:', library=ROOT / 'runtime-deps/native' / name)
        try:
            db.execute('SELECT * FROM sqlite_dbpage LIMIT 0')
            if name == 'sqlcipher-qq-pages.dll':
                from collector.native_delta import _register
                _register(db)
                db.execute('SELECT * FROM sqlite_dbdata LIMIT 0')
        finally:
            db.close()
    if not runtime_status()['installed']:
        raise RuntimeError('Pi runtime is missing')
    child = subprocess.run([sys.executable, '-c', 'import collector, processor, messaging, automation, tools.console.app'],
        cwd=ROOT, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if child.returncode:
        raise RuntimeError('后台子进程无法加载项目模块，请重新运行 Install.cmd 修复运行路径。')
    print('Python、原生 SQLCipher、微信取钥组件、Node/Pi 和面板依赖已就绪。')


def configure_model(root):
    doc = read_config(root, 'processor')
    model = doc['model']
    model['base_url'] = ask('模型接口地址', model['base_url'])
    model['model'] = ask('模型名称', model['model'])
    print('Pi 默认沿用当前项目设置：原生压缩、附件工具和自动分组。上下文/输出预算必须符合模型服务限制。')
    for key, label in [('context_window', '模型上下文上限 tokens'), ('max_output_tokens', '最大输出 tokens')]:
        model[key] = int(ask(label, str(model[key])))
    for key, label in [('compaction_reserve_tokens', 'Pi 压缩预留 tokens'), ('compaction_keep_recent_tokens', 'Pi 保留近期 tokens')]:
        doc['agent'][key] = int(ask(label, str(doc['agent'][key])))
    model['vision'] = yes('模型是否支持图片输入', bool(model['vision']))
    from dotenv import dotenv_values
    existing = os.environ.get('CHAT_PROCESSOR_API_KEY') or dotenv_values(root / '.env').get('CHAT_PROCESSOR_API_KEY')
    value = getpass.getpass('API Key（隐藏输入；已有密钥时回车保留）：')
    if value:
        secret(root, 'CHAT_PROCESSOR_API_KEY', value)
    elif not existing:
        raise ValueError('模型密钥尚未填写')
    model['api_key_env'] = 'CHAT_PROCESSOR_API_KEY'
    model['api_key_file'] = ''
    save_config(root, 'processor', doc)
    from processor import load_config
    load_config(root / 'config/processor.toml')


def configure_console(root):
    import socket
    import tomlkit
    path = root / 'tools/console/console.toml'
    doc = tomlkit.loads(path.read_text(encoding='utf-8'))
    def available(port):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
                return True
            except OSError:
                return False
    default = int(doc.get('port', 10400))
    if not available(default):
        print('默认面板端口已被占用，请为此安装选择独立端口。')
        default = 10411
    port = int(ask('调试台端口（同机多套环境需不同端口）', str(default)))
    if not 1024 <= port <= 65535 or not available(port):
        raise RuntimeError('端口无效或已被占用；请重新运行 Configure.cmd 选择其他端口。')
    doc['port'] = port
    pending = path.with_suffix('.tmp')
    pending.write_text(tomlkit.dumps(doc), encoding='utf-8')
    os.replace(pending, path)


def configure_collector(root, platform):
    name = '微信' if platform == 'wechat' else 'QQ'
    options = [(str(p), str(p)) for p in discover_accounts(platform)]
    options.append(('手动输入数据库目录', ''))
    chosen = choose(options, f'{name}数据库位置')
    folder = Path(chosen or ask('输入 db_storage 目录' if platform == 'wechat' else '输入 nt_db 目录')).resolve(strict=True)
    expected = 'db_storage' if platform == 'wechat' else 'nt_db'
    if folder.name != expected:
        raise ValueError('请选择 ' + expected + ' 目录')
    account = folder.parent.name.rsplit('_', 1)[0] if platform == 'wechat' else folder.parent.parent.name
    doc = read_config(root, platform)
    source = doc['source']
    source['account_id'] = ask('账号 ID（确认对应登录账号）', account)
    source['encrypted_dir'] = str(folder)
    source['media_dir'] = str(folder.parent if platform == 'wechat' else folder.parent / 'nt_data')
    source['key_file'] = '../secrets/wechat-keys.json' if platform == 'wechat' else '../secrets/account-key.json'
    doc['schedule']['initial_lookback_seconds'] = 7 * 86400
    doc['schedule']['interval_seconds'] = int(ask('采集检查间隔（秒）', '1800'))
    doc['schedule']['refresh_interval_seconds'] = int(ask('数据库刷新最短间隔（秒，0 表示每轮）', '0'))
    doc['processing']['intensity'] = choose([('轻量：只保留附件索引', 'metadata'), ('标准：图片等媒体', 'standard'), ('完整：媒体与附件归档', 'full')], '抓取解码强度')
    if yes('是否已有经过验证的密钥 JSON 文件'):
        source['key_file'] = str(Path(ask('密钥文件路径')).resolve(strict=True))
    if platform == 'wechat':
        print('图片密钥与数据库密钥不同。若已有包含 image_key_verified 的凭据或图片归档，可在此复用。')
        cache = ask('已有明文图片缓存目录（可留空）')
        decoder = ask('已有 VoipEngine.dll 路径（仅 wxgf 图片需要，可留空）')
        if cache:
            source['image_cache_dir'] = str(Path(cache).resolve(strict=True))
        if decoder:
            source['decoder_dll'] = str(Path(decoder).resolve(strict=True))
    save_config(root, platform, doc)
    from collector.config import load_config
    load_config(root / 'config' / (platform + '.toml'))


def recover_key(root, platform):
    from collector.config import load_config
    config = load_config(root / 'config' / (platform + '.toml'))
    if Path(config['source']['key_file']).is_file():
        return
    script = root / ('tools/qq_recover.py' if platform == 'qq' else 'tools/install/recover_wechat.py')
    print('请保持对应客户端登录。取钥只保存本机。' + ('微信原生 Hook 需要重启登录。' if platform == 'wechat' else 'QQ 只读取钥，无需重启。'))
    if not ctypes.windll.shell32.IsUserAnAdmin():
        # Elevate this explicit, interactive key-recovery step only; never unattended workers.
        command = "$p=Start-Process -FilePath $env:CC_PYTHON -ArgumentList ('\"'+$env:CC_RECOVER+'\"') -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
        env = dict(os.environ, CC_PYTHON=sys.executable, CC_RECOVER=str(script))
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        result = subprocess.run([sys.executable, str(script)])
    if platform == 'qq' and result.returncode == 2:
        raise RuntimeError('QQ 正在重写数据库，凭据尚未验证；请保持 QQ 登录，稍后重新运行 Configure.cmd 选择继续。已完成步骤不会重做。')
    if result.returncode or not Path(config['source']['key_file']).is_file():
        raise RuntimeError('取钥未完成；请确认管理员授权、登录状态及客户端版本，重新运行 Configure.cmd。')


def configure_feishu(root, state, save):
    from messaging import load_config, feishu
    from messaging.management import send_test, test_history
    doc = read_config(root, 'messaging')
    target = next(t for t in doc['targets'] if t['id'] == 'feishu-assistant')
    if not state.get('feishu_credentials'):
        print('飞书：可在官方网页创建/复用应用，或使用已有应用凭据。')
        if yes('通过飞书官方页面一键配置应用', True):
            import lark_oapi
            def show_qr(info):
                print('已打开飞书官方授权页，请在浏览器完成授权；如未打开，可复制：\n' + info['url'])
                webbrowser.open(info['url'])
            async def register():
                return await asyncio.wait_for(lark_oapi.aregister_app(
                    on_qr_code=show_qr, source='chat-collector', app_preset={'name': '聊天待办助手'},
                    addons={'preset': False, 'scopes': {'tenant': ['im:message:send_as_bot', 'im:resource', 'im:chat:read', 'im:chat.members:read']},
                            'callbacks': {'items': ['card.action.trigger']}}), timeout=660)
            credentials = asyncio.run(register())
            app_id, app_secret = credentials['client_id'], credentials['client_secret']
            state['operator'] = (credentials.get('user_info') or {}).get('open_id', '')
        else:
            app_id = ask('App ID')
            app_secret = getpass.getpass('App Secret（隐藏输入）：')
        secret(root, 'CHAT_FEISHU_APP_ID', app_id)
        secret(root, 'CHAT_FEISHU_APP_SECRET', app_secret)
        state['feishu_credentials'] = True
        save()
    from dotenv import dotenv_values
    app_id = os.environ.get('CHAT_FEISHU_APP_ID') or dotenv_values(root / '.env')['CHAT_FEISHU_APP_ID']
    print('请在应用后台开启机器人、确认权限并发布版本，将机器人加入专用群。')
    webbrowser.open('https://open.feishu.cn/app/' + app_id)
    ask('完成后按回车继续')
    config = load_config(root / 'config/messaging.toml')
    if not state.get('feishu_target'):
        groups = feishu.list_chats(config, target)
        if not groups['complete']:
            raise RuntimeError('平台未返回完整群列表，请检查可用范围后重试。')
        target['recipient_id'] = choose([(g['name'] + ' ' + g['chat_id'], g['chat_id']) for g in groups['items']], '选择待办群')
        members = feishu.list_chats(config, target, chat_id=target['recipient_id'])
        operator = state.get('operator')
        if operator and any(m['open_id'] == operator for m in members['items']):
            target['allowed_operator_ids'] = [operator]
        else:
            target['allowed_operator_ids'] = [choose([(m['name'] + ' ' + m['open_id'], m['open_id']) for m in members['items']], '选择允许操作待办的本人账号')]
        save_config(root, 'messaging', doc)
        state['feishu_target'] = True
        save()
    # Connect the real callback handler before the user saves long-connection settings.
    doc['enabled'] = True
    target['enabled'] = True
    save_config(root, 'messaging', doc)
    stop = root / 'data/install/listener.stop'
    stop.unlink(missing_ok=True)
    child = subprocess.Popen([sys.executable, '-m', 'messaging.feishu', 'listen', '--config', str(root / 'config/messaging.toml'),
        '--target', 'feishu-assistant', '--stop-file', str(stop)], cwd=root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        print('本机已启动回调连接。后台 → 事件与回调 → 回调配置：长连接，添加“卡片回传交互”，保存并发布新版本。')
        ask('发布后按回车继续')
        if child.poll() is not None:
            raise RuntimeError('长连接程序退出，请检查应用凭据、网络与发布状态。')
        identifier = state.get('feishu_test')
        if not identifier:
            if not yes('向所选群发送合成卡片和测试文件并验证按钮', True):
                raise RuntimeError('飞书测试尚未完成；配置已保存，可重新运行引导。')
            # Only the explicit synthetic sender is used here, never the formal delivery queue.
            doc['enabled'] = True
            target['enabled'] = True
            save_config(root, 'messaging', doc)
            result = send_test(root / 'config/messaging.toml', target['id'])
            state['feishu_test'] = result['test_id']
            save()
            if result['status'] != 'sent':
                raise RuntimeError('测试投递失败或结果未知，请先在群和调试台核对，避免重复发送。')
            identifier = result['test_id']
        print('请点击群中测试卡片按钮，正在等待真实回调（最多 3 分钟）……')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            record = next((r for r in test_history(root / 'config/messaging.toml', limit=100) if r['test_id'] == identifier), None)
            if record and record.get('callback', {}).get('status') == 'test_received':
                return
            if child.poll() is not None:
                raise RuntimeError('回调连接已退出，请检查网络后重试。')
            time.sleep(1)
        raise RuntimeError('未收到真实按钮回调；请核对回调配置及发布版本后重试，不需要重新发测试卡片。')
    finally:
        stop.touch()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            raise RuntimeError('安装回调监听尚未安全退出，暂不启动正式监听。')


def first_run(root, state, save):
    from collector import __main__ as collect
    from collector.config import load_config as load_collector
    from processor import load_config, run_round
    from messaging import load_config as load_message, sync
    for platform in state['platforms']:
        stage = 'collected_' + platform
        if state.get(stage):
            continue
        recover_key(root, platform)
        print(platform + '：首次解密并筛选最近 7 日；完成后保存断点……', flush=True)
        config = load_collector(root / 'config' / (platform + '.toml'))
        end = state.get('end_at', state['start_at'] + 7 * 86400)
        result = collect.run_round(config, start=state['start_at'], end=end)
        if result.get('status') in {'no_enabled_rules', 'waiting_for_refresh'}:
            raise RuntimeError('采集尚未完成，请检查规则与刷新设置。')
        # Replay exports durably first; only then establish this fresh installation's incremental boundary.
        from collector.store import Store
        with collect.InstanceLock(config['storage']['state_file']):
            store = Store(config['storage']['state_file'], platform, config['source']['account_id'])
            try:
                with store.db:
                    store.set('window_end', max(int(store.get('window_end') or 0), end))
            finally:
                store.close()
        state[stage] = True
        save()
    if not state.get('processed'):
        print('按最近 7 日筛选结果提取待办；会调用已配置模型，附件按需读取。', flush=True)
        config = load_config(root / 'config/processor.toml')
        while True:
            result = run_round(config)
            remaining = result['pending_groups']
            print('本轮结束，剩余分组：' + str(remaining), flush=True)
            if remaining == 0:
                break
            if result.get('processed_groups', 0) == 0:
                raise RuntimeError('处理没有推进，请检查本地处理状态后重试。')
        state['processed'] = True
        save()
    sync(load_message(root / 'config/messaging.toml'))


def startup(root, enabled):
    from tools.install.startup import set_startup
    return set_startup(root, enabled)


def wizard(root=ROOT):
    from collector.__main__ import InstanceLock
    state_path = root / 'data/install/progress.json'
    with InstanceLock(root / 'data/install/setup'):
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        def save():
            atomic_json(state_path, state)
        if (root / 'data/install/ready.json').exists():
            action = choose([('启动所有已配置服务', 'start'), ('调整模型配置', 'model'), ('设置登录启动', 'startup')], '此安装已完成')
            if action == 'model':
                configure_model(root)
                print('已保存模型配置；正在运行的自动服务下一轮读取新配置。')
            elif action == 'startup':
                startup(root, yes('当前用户登录 Windows 后自动启动'))
            else:
                from tools.install.launcher import start_all
                print(start_all(root, open_browser=True))
            return
        if state:
            options = [('继续上次未完成的步骤', 'continue'), ('调整模型配置', 'model')]
            options += [('修改 ' + p + ' 路径或密钥文件', p) for p in state.get('platforms', []) if not state.get('collected_' + p)]
            action = choose(options, '检测到上次安装进度')
            if action == 'model':
                configure_model(root)
                state['model'] = True
                state.pop('processed', None)
                save()
            elif action in ('wechat', 'qq'):
                configure_collector(root, action)
                state['configured_' + action] = True
                save()
        if not state:
            if (root / 'config/processor.toml').exists():
                raise RuntimeError('发现已有部署。首次引导不会覆盖现有配置和历史，请使用新安装目录。')
            now = int(time.time())
            state.update(start_at=now - 7 * 86400, end_at=now, initialized=False)
            save()
        if not state.get('initialized'):
            (root / 'config').mkdir(exist_ok=True)
            for template in (root / 'tools/install/defaults').glob('*.toml'):
                if not (root / 'config' / template.name).exists():
                    shutil.copyfile(template, root / 'config' / template.name)
            doc = read_config(root, 'processor')
            doc['input']['start_at'] = state['start_at']
            save_config(root, 'processor', doc)
            state['initialized'] = True
            save()
        secure_secrets(root)
        if not state.get('console'):
            configure_console(root)
            state['console'] = True
            save()
        if 'platforms' not in state:
            state['platforms'] = choose([('微信和 QQ', ['wechat', 'qq']), ('仅微信', ['wechat']), ('仅 QQ', ['qq'])], '采集哪些平台')
            save()
        for platform in state['platforms']:
            if not state.get('configured_' + platform):
                configure_collector(root, platform)
                state['configured_' + platform] = True
                save()
        if not state.get('model'):
            configure_model(root)
            state['model'] = True
            save()
        if 'feishu' not in state:
            state['feishu'] = yes('配置飞书机器人并投递待办', True)
            save()
        if state['feishu'] and not state.get('feishu_verified'):
            configure_feishu(root, state, save)
            state['feishu_verified'] = True
            save()
        if not state.get('first_run_approved'):
            print('首次处理将把筛选后的最近 7 日聊天及按需读取的附件发送到模型接口，可能产生费用。')
            if not yes('现在开始首次解密和待办处理', True):
                print('配置已保存。重新运行 Configure.cmd 可继续。')
                return
            state['first_run_approved'] = True
            save()
        first_run(root, state, save)
        auto = read_config(root, 'automation')
        auto['enabled'] = True
        for platform in ('wechat', 'qq'):
            auto['jobs'][platform]['enabled'] = platform in state['platforms']
            auto['jobs'][platform]['interval_seconds'] = read_config(root, platform)['schedule']['interval_seconds']
        auto['jobs']['processor']['enabled'] = True
        auto['jobs']['processor']['interval_seconds'] = int(ask('自动待办处理检查间隔（秒）', '1800'))
        for name in ('messaging', 'feedback'):
            auto['jobs'][name]['enabled'] = state['feishu']
        auto['listener']['enabled'] = state['feishu']
        auto['alerts']['enabled'] = state['feishu']
        save_config(root, 'automation', auto)
        from automation import load_config
        load_config(root / 'config/automation.toml')
        startup(root, yes('当前用户登录 Windows 后自动启动（后台无弹窗）'))
        atomic_json(root / 'data/install/ready.json', {'completed_at': int(time.time()), 'start_at': state['start_at'], 'platforms': state['platforms']})
        from tools.install.launcher import start_all
        result = start_all(root, open_browser=True)
        print('首次处理完成，自动服务已启动：' + result['console'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-runtime', action='store_true')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)  # SDK errors can contain credentials; show stage/type instead.
    try:
        if args.check_runtime:
            check_runtime()
        else:
            wizard()
    except (Exception, KeyboardInterrupt) as exc:
        print('\n未完成，已保存的配置和进度会保留。')
        print(str(exc) if isinstance(exc, RuntimeError) else '错误类型：' + type(exc).__name__)
        print('修正后重新运行 Configure.cmd；不要删除 data 或 secrets。')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
