"""Prepare a tagged Windows preview without copying any local installation state."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

NATIVE_TAG = 'v1.1.0'
NATIVE_SHA256 = '477b07f983ed8c9241ba26cc59f4d83ba42b8f232c6cf5b614d2c97b71f7a23c'
NATIVE_FILES = (
    'native/build.json', 'native/dbdata-build.json', 'native/dbdata.c',
    'native/libcrypto-3-x64__.dll', 'native/sqlcipher-pages.dll',
    'native/sqlcipher-qq-pages.dll', 'native/sqlite-dbdata-qq.dll', 'native/sqlite3.c',
    'sqlcipher3/__init__.py', 'sqlcipher3/_sqlite3.cp313-win_amd64.pyd', 'sqlcipher3/dbapi2.py',
    'sqlcipher3-0.6.5.dist-info/INSTALLER', 'sqlcipher3-0.6.5.dist-info/METADATA',
    'sqlcipher3-0.6.5.dist-info/WHEEL', 'sqlcipher3-0.6.5.dist-info/licenses/LICENSE',
    'sqlcipher3-0.6.5.dist-info/top_level.txt',
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git(source, *args):
    return subprocess.check_output(['git', *args], cwd=source, text=True).strip()


def release_notes(changelog, tag, version):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag) or tag != 'v' + version:
        raise ValueError('Tag must match version.json (vX.Y.Z)')
    sections = re.split(r'(?m)(?=^## )', changelog)
    matching = [section.strip() for section in sections
                if re.match(r'^## ' + re.escape(version) + r' - \d{4}-\d{2}-\d{2}\s*$', section.splitlines()[0] if section else '')]
    if len(matching) != 1 or not '\n'.join(matching[0].splitlines()[1:]).strip():
        raise ValueError('CHANGELOG.md must contain exactly one nonempty version section')
    return matching[0] + '\n'


def restore_native(source, archive):
    if sha256(archive.read_bytes()) != NATIVE_SHA256:
        raise ValueError('Native baseline ZIP checksum mismatch')
    if subprocess.run(['git', 'diff', '--quiet', NATIVE_TAG, 'HEAD', '--', 'tools/build_sqlcipher_pages.py'], cwd=source).returncode:
        raise ValueError('Native build changed; review and update the pinned native baseline before publishing')
    target = source / 'runtime-deps'
    if target.exists():
        raise ValueError('Native restore requires a fresh checkout')
    # Reuse the exact approved dependency bytes, never the previous release source or configuration.
    with zipfile.ZipFile(archive) as bundle:
        for name in NATIVE_FILES:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bundle.read('chat-collector/runtime-deps/' + name))


def verify_bundle(output, version, commit):
    with zipfile.ZipFile(output) as bundle:
        manifest = json.loads(bundle.read('chat-collector/release-manifest.json'))
        if (manifest['source_commit'], manifest['version'], manifest['working_tree_dirty']) != (commit, version, False):
            raise ValueError('Release manifest does not identify the clean tagged source')
        expected = {'chat-collector/release-manifest.json'}
        for file in manifest['files']:
            name = file['path']
            parts = PurePosixPath(name).parts
            if (not parts or PurePosixPath(name).is_absolute() or '\\' in name or ':' in name or
                    set(parts) & {'..', '.git', '.runtime', 'data', 'config', 'secrets', 'node_modules', '__pycache__'} or
                    any(part.startswith('.env') and part != '.env.example' for part in parts) or name == 'tools/console/console.toml'):
                raise ValueError('Unexpected private or unsafe package path: ' + name)
            archive_name = 'chat-collector/' + name
            if archive_name in expected or sha256(bundle.read(archive_name)) != file['sha256']:
                raise ValueError('Duplicate or invalid package file: ' + name)
            expected.add(archive_name)
        required = {'chat-collector/THIRD_PARTY_NOTICES.md', 'chat-collector/third_party/provenance.json',
                    'chat-collector/Install.cmd', 'chat-collector/version.json'}
        required.update('chat-collector/runtime-deps/' + name for name in NATIVE_FILES)
        required.update('chat-collector/third_party/licenses/' + name for name in (
            'OpenSSL-Apache-2.0.txt', 'OpenSSL-NOTICE.txt', 'Pi-MIT.txt', 'PyMuPDF-AGPL-3.0.txt',
            'SQLCipher-source-notices.txt', 'TypeBox-MIT.txt', 'sqlcipher3-Zlib.txt'))
        if set(bundle.namelist()) != expected or len(bundle.namelist()) != len(expected) or not required <= expected:
            raise ValueError('Package inventory mismatch')
    if output.with_suffix('.zip.sha256').read_text().strip() != sha256(output.read_bytes()) + '  ' + output.name:
        raise ValueError('Published ZIP checksum mismatch')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--native-archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    version = json.loads((source / 'version.json').read_text(encoding='utf-8'))['version']
    notes = release_notes((source / 'CHANGELOG.md').read_text(encoding='utf-8'), args.tag, version)
    commit = git(source, 'rev-parse', 'HEAD')
    if git(source, 'status', '--porcelain') or git(source, 'rev-parse', f'refs/tags/{args.tag}^{{commit}}') != commit:
        raise ValueError('Build requires a clean checkout of the requested tag')
    if subprocess.run(['git', 'merge-base', '--is-ancestor', commit, 'origin/main'], cwd=source).returncode:
        raise ValueError('Release tag must identify a commit on main')
    restore_native(source, args.native_archive)
    bundle = output / f'chat-collector-{version}-windows-x64-preview.zip'
    subprocess.run([sys.executable, str(source / 'tools/install/build_release.py'), '--output', str(bundle)], cwd=source, check=True)
    verify_bundle(bundle, version, commit)
    (output / 'release-notes.md').write_text(notes + '\n---\n\n'
        'Windows x64 本地使用预览版。真实客户端兼容性未全面验证；已有部署请先备份配置和数据。'
        '项目级再分发许可尚未完成，详见包内 `THIRD_PARTY_NOTICES.md`。\n', encoding='utf-8')


if __name__ == '__main__':
    main()
