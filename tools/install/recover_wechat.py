"""Use upstream wx_key native hook, then validate a disposable DB before saving."""
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime-deps')]


def recover():
    import ctypes
    import psutil
    import wx_key
    from collector.config import load_config
    from collector.vendor.wechat.decrypt import WeChatDatabaseDecryptor
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('请以管理员身份运行 Configure.cmd 后重试取钥。')
    source = load_config(ROOT / 'config/wechat.toml')['source']
    processes = [p for p in psutil.process_iter(['name', 'exe', 'memory_info'])
                 if (p.info['name'] or '').lower() in {'weixin.exe', 'wechat.exe'}]
    if not processes:
        raise RuntimeError('请先打开微信并登录，再重试取钥。')
    main = max(processes, key=lambda p: p.info['memory_info'].rss)
    executable = main.info['exe']
    if not executable or not Path(executable).is_file():
        raise RuntimeError('无法定位微信程序。')
    print('本步骤将退出并重新打开微信，请完成登录；密钥只保存本机。', flush=True)
    if input('输入 YES 允许重启微信，其他输入取消：').strip() != 'YES':
        raise RuntimeError('已取消取钥，原凭据未改变。')
    for proc in processes:
        proc.terminate()
    _, alive = psutil.wait_procs(processes, timeout=10)
    if alive:
        raise RuntimeError('微信尚未退出，请手动退出后重试。')
    subprocess.Popen([executable])  # Explicit interactive login requested above.
    deadline = time.monotonic() + 20
    target = None
    while time.monotonic() < deadline:
        candidates = []
        for p in psutil.process_iter(['exe', 'memory_info']):
            if p.info['exe'] and os.path.normcase(p.info['exe']) == os.path.normcase(executable):
                candidates.append(p)
        if candidates:
            target = max(candidates, key=lambda p: p.info['memory_info'].rss)
            break
        time.sleep(.2)
    if not target or not wx_key.initialize_hook(target.pid):
        raise RuntimeError('原生取钥组件无法连接此版本微信；可改用已有密钥文件。')
    key = None
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            found = wx_key.poll_key_data()
            if found and found.get('key'):
                key = found['key']
                break
            time.sleep(.1)
    finally:
        wx_key.cleanup_hook()
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', key):
        raise RuntimeError('未取得有效凭据，请确认登录或提供已有密钥文件。')
    probe = Path(source['encrypted_dir']) / 'contact/contact.db'
    if not probe.is_file():
        raise RuntimeError('缺少 contact/contact.db，无法核验账号与凭据。')
    destination = Path(source['key_file'])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='wechat-verify-') as temp:
        if not WeChatDatabaseDecryptor(key).decrypt_database(str(probe), str(Path(temp) / 'contact.db')):
            raise RuntimeError('候选凭据未通过解密验证；原凭据未改变。')
    previous = json.loads(destination.read_text(encoding='utf-8')) if destination.exists() else {}
    previous.setdefault(source['account_id'], {})['db_key'] = key
    pending = destination.with_suffix('.tmp')
    pending.write_text(json.dumps(previous), encoding='utf-8')
    os.replace(pending, destination)
    print('微信凭据已验证并保存在本机。')


if __name__ == '__main__':
    try:
        recover()
    except Exception as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else '取钥未完成：' + type(exc).__name__)
        raise SystemExit(1)
