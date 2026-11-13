"""Current-user Windows login entry, shared by installation and the console."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys


RUN = r'Software\Microsoft\Windows\CurrentVersion\Run'
APPROVED = r'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'


def _entry(root):
    root = Path(root).resolve()
    private = root / '.runtime/python/pythonw.exe'
    executable = private if private.is_file() else Path(sys.executable).with_name('pythonw.exe')
    name = 'ChatCollector-' + hashlib.sha256(str(root).lower().encode()).hexdigest()[:12]
    return name, subprocess.list2cmdline([str(executable), str(root / 'tools/install/launcher.py')]), executable


def _read(registry, branch, name):
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, branch) as key:
            return registry.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return None


def startup_status(root):
    if os.name != 'nt':
        return {'supported': False, 'enabled': False, 'installed': False, 'matches': False}
    import winreg
    name, command, _ = _entry(root)
    try:
        value = _read(winreg, RUN, name)
        approved = _read(winreg, APPROVED, name)
    except OSError:
        raise RuntimeError('无法读取当前用户的 Windows 启动项，请检查系统权限。') from None
    blocked = isinstance(approved, bytes) and bool(approved) and approved[0] in (3, 7)
    return {'supported': True, 'installed': value is not None, 'matches': value == command,
            'enabled': value == command and not blocked, 'blocked_by_windows': blocked, 'entry_name': name}


def set_startup(root, enabled):
    if type(enabled) is not bool:
        raise ValueError('enabled must be boolean')
    if os.name != 'nt':
        raise RuntimeError('登录启动项仅支持 Windows。')
    import winreg
    name, command, executable = _entry(root)
    if enabled and (not executable.is_file() or not (Path(root) / 'tools/install/launcher.py').is_file()):
        raise RuntimeError('启动程序尚未安装完整，请先完成安装。')
    if enabled and len(command) > 260:
        raise RuntimeError('安装路径过长，超过 Windows 登录启动项限制；请使用较短的安装目录。')
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
        # Explicit user enable/disable clears Task Manager's old decision for this entry only.
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass
    except OSError:
        raise RuntimeError('Windows 未允许修改启动项，请检查当前用户权限后刷新状态。') from None
    result = startup_status(root)
    if result['enabled'] != enabled:
        raise RuntimeError('启动项未达到请求状态，请刷新核对。')
    return result
