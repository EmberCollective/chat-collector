import subprocess
import sys


def test_common_helpers_do_not_import_pipeline():
    result = subprocess.run([sys.executable, '-c',
        'import sys; from common.locking import InstanceLock; from common.codec import digest; '
        'assert len(digest({"a": 1})) == 64; '
        'assert not any(k.split(".")[0] in {"collector", "processor", "messaging"} for k in sys.modules)'],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_legacy_helpers_keep_identity():
    from collector.__main__ import InstanceLock as old_lock
    from collector.store import canonical as old_canonical, digest as old_digest
    from common.locking import InstanceLock
    from common.codec import canonical, digest
    assert old_lock is InstanceLock
    assert old_canonical is canonical and old_digest is digest
