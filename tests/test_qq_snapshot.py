from pathlib import Path

import pytest

from collector.adapters import qq


def fixture(tmp_path):
    source = tmp_path / "nt_msg.db"
    source.write_bytes(bytes(1024) + b"main" * 1024)
    wal = Path(str(source) + "-wal")
    wal.write_bytes(b"captured-prefix")

    def stamp():
        result = []
        for p in (source, wal):
            s = p.stat()
            result.append([str(p), s.st_size, s.st_mtime_ns])
        return result

    return source, wal, stamp


def test_wal_append_is_allowed_and_cutoff_is_fixed(tmp_path, monkeypatch):
    source, wal, stamp = fixture(tmp_path)
    real = qq._prefix_digest

    def appending(stream, size, destination=None):
        result = real(stream, size, destination)
        if destination is not None:
            with wal.open("ab") as dst:
                dst.write(b"later-transaction")
        return result

    monkeypatch.setattr(qq, "_prefix_digest", appending)
    target = tmp_path / "copy.db"
    captured = qq._capture_snapshot(source, target, stamp)
    assert Path(str(target) + "-wal").read_bytes() == b"captured-prefix"
    assert captured[1][1] == len(b"captured-prefix")
    assert target.read_bytes() == b"main" * 1024


def test_rewritten_wal_prefix_is_rejected(tmp_path, monkeypatch):
    source, wal, stamp = fixture(tmp_path)
    real = qq._prefix_digest

    def rewriting(stream, size, destination=None):
        result = real(stream, size, destination)
        if destination is not None:
            with wal.open("r+b") as dst:
                current = dst.read(1)
                dst.seek(0)
                dst.write(b"x" if current != b"x" else b"y")
        return result

    monkeypatch.setattr(qq, "_prefix_digest", rewriting)
    with pytest.raises(RuntimeError, match="checkpointed or replaced"):
        qq._capture_snapshot(source, tmp_path / "copy.db", stamp)
