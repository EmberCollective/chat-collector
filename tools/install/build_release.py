"""Build a Windows x64 local-use preview from an explicit file allowlist."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = ('collector', 'providers', 'rulesets', 'processor', 'messaging', 'automation',
               'processing-rules', 'docs', 'tools/console', 'tools/automation', 'tools/install')
SOURCE_FILES = ('contracts.py', 'README.md', 'FILTERS.md', '.env.example', 'requirements.txt',
                'Install.cmd', 'Start.cmd', 'Configure.cmd', 'tools/qq_recover.py', 'tools/qq-recover.cmd',
                'tools/qq-nt-export/scripts/ntqq_key_scan.py', 'tools/qq-nt-export/LICENSE')
EXCLUDED = {'__pycache__', 'node_modules', '.runtime', '.pytest_cache', 'tests', 'node'}


def payload(root):
    files = {}
    for directory in SOURCE_DIRS:
        for folder, dirs, names in os.walk(root / directory):
            dirs[:] = [name for name in dirs if name not in EXCLUDED]
            for name in names:
                p = Path(folder) / name
                if p.suffix not in {'.pyc', '.log'}:
                    files[p.relative_to(root).as_posix()] = p.read_bytes()
    for name in SOURCE_FILES:
        files[name] = (root / name).read_bytes()
    for name in ('sqlcipher-pages.dll', 'sqlcipher-qq-pages.dll', 'sqlite-dbdata-qq.dll',
                 'libcrypto-3-x64__.dll', 'sqlite3.c', 'dbdata.c', 'build.json'):
        path = 'runtime-deps/native/' + name
        files[path] = (root / path).read_bytes()
    meta = json.loads((root / 'runtime-deps/native/dbdata-build.json').read_text())
    meta.pop('commands', None)  # Local compiler paths are not distribution metadata.
    files['runtime-deps/native/dbdata-build.json'] = json.dumps(meta, indent=2).encode()
    for folder in ('sqlcipher3', 'sqlcipher3-0.6.5.dist-info'):
        for p in (root / 'runtime-deps' / folder).rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and p.name not in {'direct_url.json', 'RECORD', 'REQUESTED'}:
                files[p.relative_to(root).as_posix()] = p.read_bytes()
    for name in files:
        if name.endswith(('.cmd', '.ps1')):
            files[name] = files[name].replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
    return files


def build(root, output):
    files = payload(root)
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root))
    manifest = {'format': 1, 'platform': 'windows-x64', 'python': '3.13.8',
                'source_commit': sha, 'working_tree_dirty': dirty, 'release': 'local-use-preview',
                'files': [{'path': name, 'sha256': hashlib.sha256(data).hexdigest()} for name, data in sorted(files.items())]}
    files['release-manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(files.items()):
            archive.writestr('chat-collector/' + name, data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest + '  ' + output.name + '\n')
    return {'artifact': str(output), 'sha256': digest, 'files': len(files), 'source_commit': sha, 'working_tree_dirty': dirty}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/chat-collector-windows-x64-preview.zip')
    args = parser.parse_args()
    print(json.dumps(build(ROOT, args.output), indent=2))
