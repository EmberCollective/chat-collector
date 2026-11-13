import hashlib
import io
import json
import sqlite3
import struct
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from PIL import Image
import pytest

from collector.assets import AttachmentArchive


def test_native_decoder_config_matches_observed_32_byte_abi():
    import ctypes
    from collector.asset_vendor import _WxAMConfig
    config = _WxAMConfig()
    assert ctypes.sizeof(config) == 32
    assert bytes(config) == bytes(32)


def config(tmp_path, platform="wechat"):
    media = tmp_path / "media"
    media.mkdir()
    return {"source": {"media_dir": str(media), "decrypted_dir": str(tmp_path / "db")},
            "attachments": {"enabled": True, "archive_dir": str(tmp_path / "archive")}}


def message(kind="file", **attachment):
    return {"conversation_id": "group@chatroom", "message_id": "server:9", "sent_at": 1794412800,
            "source": {}, "attachments": [{"type": kind, **attachment}]}


def image_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (36, 24), "blue").save(buf, "JPEG")
    return buf.getvalue()


def test_wechat_resource_locator_does_not_replace_message_identity(tmp_path, monkeypatch):
    archive = AttachmentArchive(config(tmp_path), 'wechat', 'account')
    original_hash, resource_hash = 'a'*32, 'b'*32
    monkeypatch.setattr(archive, '_md5', lambda *args: resource_hash)
    def image(message, attachment, locator):
        assert locator == resource_hash
        return dict(archive._publish(data=image_bytes(), suffix='.jpg'), status='archived')
    monkeypatch.setattr(archive, '_image', image)
    result = archive.enrich(message('image', md5=original_hash))['attachments'][0]
    assert result['md5'] == original_hash
    assert result['resource_md5'] == resource_hash
    assert Path(result['archive_path']).read_bytes() == image_bytes()


def test_copy_is_content_addressed_verified_and_preserves_source(tmp_path):
    cfg = config(tmp_path)
    path = Path(cfg["source"]["media_dir"]) / "example.pdf"
    path.write_bytes(b"a file payload")
    archiver = AttachmentArchive(cfg, "qq", "account")
    record = message(local_path=str(path), name=path.name)
    result = archiver.enrich(record)["attachments"][0]
    assert result["status"] == "archived"
    assert Path(result["archive_path"]).read_bytes() == path.read_bytes()
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert archiver.enrich(record) == archiver.enrich(record)


def test_path_escape_is_rejected(tmp_path):
    cfg = config(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("private")
    result = AttachmentArchive(cfg, "qq", "account").enrich(message(local_path=str(outside)))["attachments"][0]
    assert result["status"] == "missing"
    assert result["reason"] == "attachment_path_outside_allowed_roots"
    assert not Path(cfg["attachments"]["archive_dir"]).exists()


def test_ambiguous_names_are_not_assigned(tmp_path):
    cfg = config(tmp_path)
    root = Path(cfg["source"]["media_dir"])
    for folder, data in [("a", b"one"), ("b", b"two")]:
        (root / folder).mkdir()
        (root / folder / "report.txt").write_bytes(data)
    result = AttachmentArchive(cfg, "qq", "account").enrich(message(name="report.txt", size_bytes=3))["attachments"][0]
    assert result["reason"] == "ambiguous_local_candidates"


@pytest.mark.parametrize('kind', ['file', 'image'])
def test_indexed_match_is_authorized_before_reading(tmp_path, monkeypatch, kind):
    cfg = config(tmp_path)
    root = Path(cfg['source']['media_dir'])
    md5 = 'a'*32
    path = root/((md5+'.jpg') if kind=='image' else 'secret.txt')
    path.write_bytes(image_bytes() if kind=='image' else b'private')
    archive = AttachmentArchive(cfg,'qq','account')
    real_allowed = archive._allowed
    monkeypatch.setattr(archive,'_allowed',lambda p: p != path and real_allowed(p))
    real_open = Path.open
    def guarded_open(p,*args,**kwargs):
        assert p != path, 'An unauthorized indexed file must never be opened'
        return real_open(p,*args,**kwargs)
    monkeypatch.setattr(Path,'open',guarded_open)
    result = archive.enrich(message(kind,name=path.name,md5=md5))['attachments'][0]
    assert result['status'] == 'missing'
    assert not Path(cfg['attachments']['archive_dir']).exists()


def test_filename_catalog_does_not_resolve_unrelated_files(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    root = Path(cfg['source']['media_dir'])
    for i in range(100):
        (root/f'unrelated-{i}.txt').write_bytes(b'unrelated')
    wanted = root/'wanted.txt'
    wanted.write_bytes(b'content')
    archive = AttachmentArchive(cfg,'qq','account')
    checked, real = [], archive._allowed
    def allowed(path):
        checked.append(path)
        return real(path)
    monkeypatch.setattr(archive,'_allowed',allowed)
    result = archive.enrich(message(name=wanted.name,size_bytes=7))['attachments'][0]
    assert result['status']=='archived' and Path(result['archive_path']).read_bytes()==b'content'
    assert wanted in checked and not any(p.name.startswith('unrelated-') for p in checked)


def test_missing_md5_uses_message_db_and_preserves_encrypted_thumb(tmp_path):
    cfg = config(tmp_path)
    root = Path(cfg["source"]["media_dir"])
    database = Path(cfg["source"]["decrypted_dir"])
    database.mkdir()
    md5 = "a" * 32
    table = "Msg_" + "b" * 32
    with sqlite3.connect(database / "message_0.db") as conn:
        conn.execute(f'CREATE TABLE "{table}"(local_id,create_time,packed_info_data)')
        conn.execute(f'INSERT INTO "{table}" VALUES(?,?,?)', (1, 1794412800, (md5 + "_t.dat").encode()))
    image = image_bytes()
    key, xor = b"0123456789abcdef", 61
    encrypted_head = AES.new(key, AES.MODE_ECB).encrypt(pad(image[:64], 16))
    tail = bytes(b ^ xor for b in image[-32:])
    encrypted = struct.pack("<6sLLx", b"\x07\x08V2\x08\x07", 64, 32) + encrypted_head + image[64:-32] + tail
    path = root / "msg/attach" / hashlib.md5(b"group@chatroom").hexdigest() / "2026-11/Img" / (md5 + "_t.dat")
    path.parent.mkdir(parents=True)
    path.write_bytes(encrypted)
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"account": {"image_key_verified": True, "image_xor_key": hex(xor), "image_aes_key": key.decode()}}))
    cfg["source"]["key_file"] = str(keys)
    record = message("image")
    record["source"] = {"database": "message_0.db", "table": table, "local_id": 1}
    result = AttachmentArchive(cfg, "wechat", "account").enrich(record)["attachments"][0]
    assert result["status"] == "thumbnail_only"
    assert result["md5"] == md5
    assert Path(result["archive_path"]).read_bytes() == image
    assert Path(result["encrypted_sources"][0]["archive_path"]).read_bytes() == encrypted
    assert path.read_bytes() == encrypted


def test_cache_quality_is_not_claimed_as_original(tmp_path):
    cfg = config(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    md5 = "c" * 32
    (cache / (md5 + ".jpg")).write_bytes(image_bytes())
    cfg["source"]["image_cache_dir"] = str(cache)
    result = AttachmentArchive(cfg, "wechat", "account").enrich(message("image", md5=md5))["attachments"][0]
    assert result["status"] == "archived"
    assert result["quality"] == "cache_quality_unknown"
    assert result["encrypted_sources"] == []


def test_unknown_qq_image_bytes_are_retained_without_claiming_decoding(tmp_path):
    cfg = config(tmp_path)
    path = Path(cfg["source"]["media_dir"]) / "photo.dat"
    path.write_bytes(b"not a decoded picture")
    result = AttachmentArchive(cfg, "qq", "account").enrich(message("image", local_path=str(path)))["attachments"][0]
    assert result["status"] == "unsupported"
    assert Path(result["archive_path"]).read_bytes() == path.read_bytes()


def test_md5_mismatch_never_archives_wrong_file(tmp_path):
    cfg = config(tmp_path)
    root = Path(cfg["source"]["media_dir"])
    path = root / "report.pdf"
    path.write_bytes(b"wrong contents")
    archive = AttachmentArchive(cfg, "qq", "account")
    for metadata in ({"local_path": str(path)}, {"name": path.name}):
        result = archive.enrich(message(md5="a" * 32, **metadata))["attachments"][0]
        assert result["status"] == "missing"
        assert "archive_path" not in result


def test_new_cached_thumbnail_does_not_hide_large_dat(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    cfg["source"]["image_cache_dir"] = str(cache)
    md5 = "d" * 32
    root = Path(cfg["source"]["media_dir"])
    dat = root / "msg/attach" / hashlib.md5(b"group@chatroom").hexdigest() / "2026-11/Img" / (md5 + "_h.dat")
    dat.parent.mkdir(parents=True)
    dat.write_bytes(b"preserved ciphertext")
    (cache / (md5 + ".jpg")).write_bytes(image_bytes())
    buffer = io.BytesIO()
    Image.new("RGB", (360, 240), "blue").save(buffer, "JPEG")
    large = buffer.getvalue()
    archive = AttachmentArchive(cfg, "wechat", "account")
    def decode(data):
        assert data == b"preserved ciphertext"
        return large, False
    monkeypatch.setattr(archive, "_decode_dat", decode)
    result = archive.enrich(message("image", md5=md5))["attachments"][0]
    assert result["dimensions"] == [360, 240]
    assert result["quality"] == "high_resolution_variant"
    assert Path(result["archive_path"]).read_bytes() == large


def test_qq_thumbnail_retained_without_claiming_original_hash(tmp_path):
    cfg = config(tmp_path)
    root = Path(cfg['source']['media_dir']) / 'Pic/2026-11/Thumb'
    root.mkdir(parents=True)
    md5 = 'a' * 32
    path = root / (md5 + '_720.jpg')
    path.write_bytes(image_bytes())
    result = AttachmentArchive(cfg, 'qq', 'account').enrich(message('image', md5=md5, size_bytes=999999))['attachments'][0]
    assert result['status'] == 'thumbnail_only'
    assert result['message_match_verified'] is False
    assert Path(result['archive_path']).read_bytes() == image_bytes()
    assert result['variants'][0]['actual_md5'] != md5


def test_downloaded_file_can_be_found_in_configured_extra_root(tmp_path):
    cfg = config(tmp_path)
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    path = downloads / 'file.pdf'
    path.write_bytes(b'known document')
    cfg['attachments']['allowed_roots'] = [str(downloads)]
    result = AttachmentArchive(cfg, 'qq', 'account').enrich(message(name='file.pdf', md5=hashlib.md5(path.read_bytes()).hexdigest()))['attachments'][0]
    assert result['status'] == 'archived'
    assert result['message_match_verified'] is True


@pytest.mark.parametrize('platform', ['wechat', 'qq'])
def test_metadata_keeps_messages_without_media_io(tmp_path, monkeypatch, platform):
    cfg = config(tmp_path)
    cfg['processing'] = {'intensity': 'metadata'}
    archive = AttachmentArchive(cfg, platform, 'account')
    def forbidden(*args):
        pytest.fail('lightweight mode accessed media')
    monkeypatch.setattr(archive, '_md5', forbidden)
    monkeypatch.setattr(archive, '_publish', forbidden)
    original = message('image', md5='a'*32, local_path='photo.dat')
    result = archive.enrich(original)
    assert result['message_id'] == original['message_id']
    assert result['attachments'][0] == dict(original['attachments'][0], status='deferred', reason='processing_intensity_metadata')
    assert 'status' not in original['attachments'][0]
    assert not archive.archive.exists()


@pytest.mark.parametrize('cached', [False, True])
def test_standard_preserves_ciphertext_and_cache_without_decrypting(tmp_path, monkeypatch, cached):
    cfg = config(tmp_path)
    cfg['processing'] = {'intensity': 'standard'}
    md5 = 'e'*32
    root = Path(cfg['source']['media_dir'])
    dat = root/'msg/attach'/hashlib.md5(b'group@chatroom').hexdigest()/'2026-11/Img'/(md5+'.dat')
    dat.parent.mkdir(parents=True)
    dat.write_bytes(b'encrypted original')
    cache = tmp_path/'cache'
    cache.mkdir()
    cfg['source']['image_cache_dir'] = str(cache)
    if cached:
        (cache/(md5+'.jpg')).write_bytes(image_bytes())
    archive = AttachmentArchive(cfg, 'wechat', 'account')
    def forbidden(*args):
        pytest.fail('standard mode attempted decryption')
    monkeypatch.setattr(archive, '_decode_dat', forbidden)
    result = archive.enrich(message('image', md5=md5))['attachments'][0]
    assert Path(result['encrypted_sources'][0]['archive_path']).read_bytes() == b'encrypted original'
    if cached:
        assert Path(result['archive_path']).read_bytes() == image_bytes()
        assert result['quality'] == 'cache_quality_unknown'
    else:
        assert result['status'] == 'encrypted_only'
        assert result['reason'] == 'processing_intensity_standard'
