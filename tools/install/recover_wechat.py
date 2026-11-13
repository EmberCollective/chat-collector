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


def connect_hook(executable, native, timeout=30):
    import psutil
    deadline = time.monotonic() + timeout
    last_error = '尚未找到微信主进程'
    # Match upstream's startup delay and shortest-command-line main-process selection.
    time.sleep(2)
    while time.monotonic() < deadline:
        candidates = [p for p in psutil.process_iter(['exe', 'cmdline'])
                      if p.info['exe'] and os.path.normcase(p.info['exe']) == os.path.normcase(executable)]
        if candidates:
            target = min(candidates, key=lambda p: len(' '.join(p.info['cmdline'] or [])))
            connected = False
            try:
                connected = bool(native.initialize_hook(target.pid))
                if connected:
                    return target.pid
                last_error = str(native.get_last_error_msg() or '原生组件尚未就绪')[:500]
            finally:
                if not connected:
                    native.cleanup_hook()
        time.sleep(.5)
    raise RuntimeError('等待微信原生连接超时，尚未进入登录取钥阶段。组件信息：' + last_error)


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
    print('本步骤将退出并重新打开微信；请等“原生连接已就绪”后再登录，密钥只保存本机。', flush=True)
    if input('输入 YES 允许重启微信，其他输入取消：').strip() != 'YES':
        raise RuntimeError('已取消取钥，原凭据未改变。')
    for proc in processes:
        proc.terminate()
    _, alive = psutil.wait_procs(processes, timeout=10)
    if alive:
        raise RuntimeError('微信尚未退出，请手动退出后重试。')
    subprocess.Popen([executable])  # Explicit interactive login requested above.
    print('正在等待微信主进程和原生连接，请暂勿点击登录……', flush=True)
    connect_hook(executable, wx_key)
    print('原生连接已就绪。请现在完成微信登录，等待取钥（最多 3 分钟）。', flush=True)
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
        raise RuntimeError('原生连接成功，但未在等待时间内取得有效凭据；请确认登录。若自动登录过快，可重试或提供已有密钥文件。')
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
