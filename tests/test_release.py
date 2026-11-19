import json
import zipfile

import pytest

from tools import release


def test_notes_include_only_the_matching_version():
    changelog = '# Notes\n\n## 1.2.3 - 2026-11-19\n\n### Fixes\n\n- first\n\n## 1.2.2 - 2026-11-18\n\n- older\n'
    assert release.release_notes(changelog, 'v1.2.3', '1.2.3') == '## 1.2.3 - 2026-11-19\n\n### Fixes\n\n- first\n'
    assert 'older' in release.release_notes(changelog, 'v1.2.2', '1.2.2')
    for tag, version in [('v1.2.2', '1.2.3'), ('v1.2.4', '1.2.4'), ('main', '1.2.3')]:
        with pytest.raises(ValueError):
            release.release_notes(changelog, tag, version)
    with pytest.raises(ValueError):
        release.release_notes(changelog + '\n## 1.2.3 - 2026-11-19\n- duplicate', 'v1.2.3', '1.2.3')


def test_native_baseline_checksum_fails_before_writing(tmp_path):
    archive = tmp_path / 'fake.zip'
    archive.write_bytes(b'fake')
    with pytest.raises(ValueError, match='checksum mismatch'):
        release.restore_native(tmp_path, archive)
    assert not (tmp_path / 'runtime-deps').exists()


def test_release_bundle_rejects_private_paths_and_wrong_source(tmp_path):
    bundle = tmp_path / 'release.zip'
    data = b'fake'
    manifest = {'version': '1.2.3', 'source_commit': 'abc', 'working_tree_dirty': False,
                'files': [{'path': '.env', 'sha256': release.sha256(data)}]}
    with zipfile.ZipFile(bundle, 'w') as archive:
        archive.writestr('chat-collector/.env', data)
        archive.writestr('chat-collector/release-manifest.json', json.dumps(manifest))
    with pytest.raises(ValueError, match='private or unsafe'):
        release.verify_bundle(bundle, '1.2.3', 'abc')
    with pytest.raises(ValueError, match='clean tagged source'):
        release.verify_bundle(bundle, '1.2.3', 'wrong')