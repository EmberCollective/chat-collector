"""Bounded discovery of SQL files in project and explicitly registered directories."""
import os
from pathlib import Path

from providers.query import inspect_rule


MAX_SQL_BYTES = 128 * 1024
MAX_TOTAL_SQL_BYTES = 2 * 1024 * 1024
MAX_RULES = 256
MAX_REGISTERED = 128
MAX_DIRECTORY_ENTRIES = 10000


def discover_rules(root, config_path, config):
    root, base = Path(root).resolve(), Path(config_path).resolve().parent
    directories, candidates, warnings = {}, {}, []
    omitted = {'registered': 0, 'rules': 0, 'directory_entries': False, 'sql_bytes': False}

    def add_directory(path, scope, blocked=False):
        path = Path(path)
        entry = directories.setdefault(path, {'path': str(path), 'scopes': []})
        if scope not in entry['scopes']:
            entry['scopes'].append(scope)
        if blocked:
            entry['error'] = '默认规则目录链接超出项目范围，未扫描'

    def add_file(path, registration=None):
        path = Path(os.path.abspath(path))
        path = path.parent.resolve() / path.name
        item = candidates.setdefault(path, {'registered_files': []})
        if registration is not None and registration not in item['registered_files']:
            item['registered_files'].append(registration)

    default = root / 'rulesets'
    try:
        target = default.resolve()
        add_directory(target if target.is_relative_to(root) else default, 'project_rulesets', not target.is_relative_to(root))
    except (OSError, RuntimeError):
        add_directory(default, 'project_rulesets')
        directories[default]['error'] = '默认规则目录无法解析，请检查目录链接'
    registrations = config.get('filters', {}).get('rulesets', [])
    if not isinstance(registrations, list):
        registrations = []
        warnings.append('注册表不是数组，未读取其文件路径')
    omitted['registered'] = max(0, len(registrations) - MAX_REGISTERED)
    for index, registration in enumerate(registrations[:MAX_REGISTERED], 1):
        file = registration.get('file') if isinstance(registration, dict) else None
        if not isinstance(file, str) or not file or Path(file).suffix.lower() != '.sql':
            warnings.append(f'注册表第 {index} 项不是 SQL 文件路径，未读取')
            continue
        try:
            path = Path(os.path.abspath(base / file))
            add_file(path, file)
            add_directory(path.parent.resolve(), 'registered_parent')
        except (OSError, ValueError, RuntimeError):
            warnings.append(f'注册表第 {index} 项路径无效，未读取')

    for path, directory in sorted(directories.items(), key=lambda item: str(item[0]).casefold()):
        if directory.get('error'):
            continue
        try:
            if not path.is_dir():
                directory['error'] = '目录不存在或不是文件夹'
                continue
            files = []
            with os.scandir(path) as entries:
                for count, entry in enumerate(entries):
                    if count >= MAX_DIRECTORY_ENTRIES:
                        omitted['directory_entries'] = True
                        directory['error'] = f'目录项超过 {MAX_DIRECTORY_ENTRIES}，剩余项目未扫描'
                        break
                    if Path(entry.name).suffix.lower() == '.sql' and not entry.is_dir(follow_symlinks=False):
                        files.append(Path(entry.path))
            for file in sorted(files, key=lambda value: (str(value).casefold(), str(value))):
                if file in candidates:
                    continue
                if len(candidates) >= MAX_RULES:
                    omitted['rules'] += 1
                else:
                    add_file(file)
        except OSError:
            directory['error'] = '目录暂不可读，请检查本机访问权限'

    rules, total_bytes = [], 0
    for path, item in sorted(candidates.items(), key=lambda pair: (str(pair[0]).casefold(), str(pair[0]))):
        try:
            file, path_mode = Path(os.path.relpath(path, base)).as_posix(), 'relative'
        except ValueError:  # Windows cannot express a relative path across drive letters.
            file, path_mode = str(path), 'absolute'
        row = {'file': file, 'path_mode': path_mode, 'exists': False, 'registered': bool(item['registered_files']),
               'registered_files': item['registered_files'], 'parameters': [], 'recent_count': False}
        rules.append(row)
        try:
            row['exists'] = path.exists() or path.is_symlink()
            parent = path.parent.resolve()
            if parent not in directories:
                row['error'] = '此目录未获准读取或未完成扫描'
                continue
            if path.is_symlink() or path.resolve().parent != parent:
                row['error'] = 'SQL 文件是链接或目标超出所在目录，未读取'
                continue
            if not path.is_file():
                row['error'] = '已注册的 SQL 文件不存在或不是普通文件' if row['registered'] else '此路径不是 SQL 文件'
                continue
            if path.stat().st_size > MAX_SQL_BYTES:
                row['error'] = f'SQL 文件超过 {MAX_SQL_BYTES} 字节，正文未读取'
                continue
            if total_bytes >= MAX_TOTAL_SQL_BYTES:
                omitted['sql_bytes'] = True
                row['error'] = '本次 SQL 正文读取总量已达上限，未读取此文件'
                continue
            with path.open('rb') as stream:
                content = stream.read(min(MAX_SQL_BYTES, MAX_TOTAL_SQL_BYTES - total_bytes) + 1)
            if len(content) > MAX_SQL_BYTES or total_bytes + len(content) > MAX_TOTAL_SQL_BYTES:
                omitted['sql_bytes'] = True
                row['error'] = 'SQL 正文读取超过单文件或本次总量限制，正文未返回'
                continue
            total_bytes += len(content)
            row['sql'] = content.decode('utf-8-sig')
            description = inspect_rule(row['sql'].strip(), path.name)
            row.update({key: value for key, value in description.items() if value is not None})
        except UnicodeError:
            row['error'] = 'SQL 文件不是有效的 UTF-8 文本'
        except (OSError, ValueError, RuntimeError):
            row['error'] = 'SQL 文件暂不可读或路径无效，请检查本机文件'
    if any(omitted.values()):
        warnings.append('发现结果有省略，请查看 omitted 和目录提示；未自动注册或启用任何规则')
    details = sorted(directories.values(), key=lambda entry: (entry['path'].casefold(), entry['path']))
    return {'directories': [entry['path'] for entry in details], 'directory_details': details, 'rules': rules,
            'warnings': warnings, 'omitted': omitted,
            'limits': {'file_bytes': MAX_SQL_BYTES, 'total_sql_bytes': MAX_TOTAL_SQL_BYTES,
                       'rules': MAX_RULES, 'registered': MAX_REGISTERED, 'directories': MAX_REGISTERED + 1,
                       'directory_entries': MAX_DIRECTORY_ENTRIES}}
