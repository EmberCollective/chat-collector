"""Archive selected message attachments without changing original media."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from collector.asset_vendor import (_decrypt_wechat_dat_v3, _decrypt_wechat_dat_v4,
                                    _extract_md5_from_packed_info, _wxgf_to_image_bytes)

_MD5 = re.compile(r"^[0-9a-fA-F]{32}$")
_TABLE = re.compile(r"^Msg_[0-9a-fA-F]{32}$")


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _image_info(data):
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return image.width * image.height, image.size, {"JPEG": ".jpg", "MPO": ".jpg"}.get(image.format, "." + image.format.lower())


class AttachmentArchive:
    """One instance per scan round; config uses [source] and [attachments]."""

    def __init__(self, config, platform, account_id):
        self.source = config.get("source", {})
        settings = config.get("attachments", {})
        self.enabled = settings.get("enabled", False)
        self.intensity = config.get("processing", {}).get("intensity", "full")
        self.platform, self.account_id = platform, str(account_id)
        self.archive = Path(settings["archive_dir"]).resolve() if self.enabled else None
        self.media = Path(self.source["media_dir"]).resolve() if self.source.get("media_dir") else None
        self.cache = Path(self.source["image_cache_dir"]).resolve() if self.source.get("image_cache_dir") else None
        self.extra_roots = [Path(p).resolve() for p in settings.get("allowed_roots", [])]
        self.roots = self.extra_roots + [p for p in (self.media, self.cache) if p is not None]
        self.indexes, self.keys = {}, None

    def _allowed(self, path):
        path = path.resolve()
        return any(path.is_relative_to(root) for root in self.roots)

    def _publish(self, path=None, data=None, suffix=""):
        if path is not None:
            path = path.resolve(strict=True)
        if path is not None and not self._allowed(path):
            raise ValueError("attachment_path_outside_allowed_roots")
        self.archive.mkdir(parents=True, exist_ok=True)
        before = path.stat() if path is not None else None
        digest = hashlib.sha256()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.archive, prefix=".asset-", delete=False) as target:
                temporary = Path(target.name)
                if path is not None:
                    with path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            target.write(chunk)
                            digest.update(chunk)
                else:
                    target.write(data)
                    digest.update(data)
                target.flush()
                os.fsync(target.fileno())
            if path is not None:
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise RuntimeError("attachment_changed_during_copy")
            sha = digest.hexdigest()
            if _digest(temporary) != sha:
                raise RuntimeError("attachment_copy_hash_mismatch")
            suffix = suffix.lower() if re.fullmatch(r"\.[a-zA-Z0-9]{1,12}", suffix) else ".bin"
            output = self.archive / sha[:2] / (sha + suffix)
            output.parent.mkdir(parents=True, exist_ok=True)
            if not output.resolve().is_relative_to(self.archive):
                raise ValueError("archive_path_outside_archive_root")
            if not output.exists() or _digest(output) != sha:
                temporary.replace(output)
            return {"archive_path": str(output), "sha256": sha, "byte_size": output.stat().st_size}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _query(self, database, sql, params=()):
        if not database or not database.is_file():
            return []
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def _md5(self, message, attachment):
        md5 = str(attachment.get("md5") or "")
        fallback = md5.lower() if _MD5.fullmatch(md5) else ""
        if self.platform != "wechat":
            return fallback
        source = message.get("source", {})
        table = source.get("table", "")
        name = Path(source.get("database", "")).name
        if _TABLE.fullmatch(table) and name:
            database = Path(self.source["decrypted_dir"]) / name
            try:
                rows = self._query(database, f'SELECT packed_info_data FROM "{table}" WHERE local_id=? AND create_time=?',
                                   (source.get("local_id"), message["sent_at"]))
                if rows:
                    md5 = _extract_md5_from_packed_info(rows[0][0])
                    if md5:
                        return md5
            except sqlite3.Error:
                pass
        resource = Path(self.source["resource_db"]) if self.source.get("resource_db") else None
        if not resource:
            return fallback
        identity = str(message.get("message_id", ""))
        server = int(identity.split(":", 1)[1]) if identity.startswith("server:") else 0
        try:
            rows = self._query(resource, '''SELECT i.packed_info FROM MessageResourceInfo i
                JOIN ChatName2Id c ON c.rowid=i.chat_id WHERE c.user_name=? AND
                ((? != 0 AND i.message_svr_id=?) OR (i.message_local_id=? AND i.message_create_time=?))
                AND i.message_local_type=? ORDER BY i.message_id DESC''',
                (message["conversation_id"], server, server, source.get("local_id"), message["sent_at"], source.get("local_type", 3)))
            hashes = {_extract_md5_from_packed_info(row[0]) for row in rows} - {""}
            return hashes.pop() if len(hashes) == 1 else fallback
        except sqlite3.Error:
            return fallback

    def _index(self, root):
        if root not in self.indexes:
            index = defaultdict(list)
            if root.is_dir() and self._allowed(root):
                for directory, dirs, files in os.walk(root, followlinks=False):
                    dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink() and self._allowed(Path(directory) / d)]
                    for name in files:
                        path = Path(directory) / name
                        # Catalog names only; resolve and authorize matched files before opening.
                        index[name.casefold()].append(path)
            self.indexes[root] = index
        return self.indexes[root]

    def _dat_candidates(self, message, md5):
        if not self.media or not md5:
            return []
        chat = hashlib.md5(message["conversation_id"].encode()).hexdigest()
        base = self.media / "msg" / "attach" / chat
        names = [md5 + suffix for suffix in ("_h.dat", ".dat", "_t.dat", ".h.dat", ".t.dat")]
        candidates = []
        if base.is_dir():
            for month in sorted(base.iterdir()):
                for name in names:
                    path = month / "Img" / name
                    if path.is_file() and self._allowed(path):
                        candidates.append(path)
        return list(dict.fromkeys(candidates))

    def _decode_dat(self, data):
        if self.keys is None:
            store = json.loads(Path(self.source["key_file"]).read_text(encoding="utf-8"))
            self.keys = store[self.account_id]
        if self.keys.get("image_key_verified") is not True:
            raise ValueError("image_key_not_verified")
        xor = int(self.keys["image_xor_key"], 16)
        if data.startswith(b"\x07\x08V2\x08\x07"):
            plain = _decrypt_wechat_dat_v4(data, xor, self.keys["image_aes_key"].encode("ascii"))
        elif data.startswith(b"\x07\x08V1\x08\x07"):
            plain = _decrypt_wechat_dat_v4(data, xor, b"cfcd208495d565ef")
        else:
            plain = _decrypt_wechat_dat_v3(data, xor)
        converted = False
        if plain.startswith(b"wxgf"):
            if not self.source.get("decoder_dll"):
                raise ValueError("wxgf_decoder_not_configured")
            plain = _wxgf_to_image_bytes(plain, self.source["decoder_dll"])
            if plain is None:
                raise ValueError("wxgf_decode_failed")
            converted = True
        return plain, converted

    def _image(self, message, attachment, md5):
        encrypted = self._dat_candidates(message, md5)
        preserved = [dict(self._publish(path=p, suffix=".dat"), original_source=str(p)) for p in encrypted]
        choices = []
        if self.cache and md5:
            for folder in (self.cache / md5[:2], self.cache):
                for suffix in (".jpg", ".png", ".gif", ".webp", ".jpeg"):
                    candidate = folder / (md5 + suffix)
                    if candidate.is_file() and self._allowed(candidate):
                        try:
                            data = candidate.read_bytes()
                            score, dimensions, ext = _image_info(data)
                            choices.append((score, data, ext, str(candidate), dimensions, "cache_quality_unknown", False))
                        except (OSError, ValueError, SyntaxError):
                            pass
        errors = []
        for path, archived in zip(encrypted, preserved) if self.intensity == "full" else ():
            try:
                # A newer cached thumbnail must not hide an older high-resolution source.
                data, converted = self._decode_dat(Path(archived["archive_path"]).read_bytes())
                score, dimensions, ext = _image_info(data)
                quality = "thumbnail" if re.search(r"[._]t\.dat$", path.name) else "high_resolution_variant" if re.search(r"[._]h\.dat$", path.name) else "standard_variant"
                choices.append((score, data, ext, str(path), dimensions, quality, converted))
            except Exception as error:
                errors.append(type(error).__name__)
        if not choices:
            return {"status": "encrypted_only" if preserved else "missing", "encrypted_sources": preserved,
                    "reason": ("processing_intensity_standard" if self.intensity == "standard" else "image_decode_unavailable") if preserved else "image_source_not_found", "decode_errors": sorted(set(errors))}
        quality_rank = {"thumbnail": 0, "cache_quality_unknown": 1, "standard_variant": 2, "high_resolution_variant": 3}
        _, data, ext, original, dimensions, quality, converted = max(choices, key=lambda x: (x[0], len(x[1]), quality_rank[x[5]]))
        only_thumb = bool(encrypted) and all(re.search(r"[._]t\.dat$", p.name) for p in encrypted)
        if only_thumb:
            quality = "thumbnail"
        return dict(self._publish(data=data, suffix=ext), status="thumbnail_only" if only_thumb or quality == "thumbnail" else "archived",
                    original_source=original, dimensions=list(dimensions), quality=quality,
                    converted_from_wxgf=converted, encrypted_sources=preserved)

    def _qq_image(self, message, attachment, md5):
        candidates = []
        local = attachment.get("local_path")
        if local:
            path = Path(local)
            path = path if path.is_absolute() or not self.media else self.media / path
            if not self._allowed(path):
                raise ValueError("attachment_path_outside_allowed_roots")
            if path.is_file():
                candidates.append(path)
        if self.media and md5:
            index_key = (self.media, "image_variants")
            if index_key not in self.indexes:
                variants = defaultdict(list)
                for name, paths in self._index(self.media).items():
                    match = re.fullmatch(r"([0-9a-f]{32})(?:_\d+)?\.(?:jpg|jpeg|png|gif|webp|bmp)", name)
                    if match:
                        variants[match[1]].extend(paths)
                self.indexes[index_key] = variants
            candidates.extend(p for p in self.indexes[index_key].get(md5, []) if self._allowed(p))
        variants, choices = [], []
        for path in sorted(set(candidates)):
            saved = dict(self._publish(path=path, suffix=path.suffix), original_source=str(path))
            data = Path(saved["archive_path"]).read_bytes()
            actual_md5 = hashlib.md5(data).hexdigest()
            verified = bool(md5) and actual_md5 == md5
            thumbnail = any(part.casefold().startswith("thumb") for part in path.parts)
            saved.update(actual_md5=actual_md5, message_match_verified=verified)
            variants.append(saved)
            try:
                pixels, dimensions, _ = _image_info(data)
                choices.append((verified, pixels, len(data), saved, dimensions, thumbnail))
            except (OSError, ValueError, SyntaxError):
                pass
        if not choices:
            return dict(variants[0], status="unsupported", reason="image_bytes_preserved_but_not_decodable", variants=variants) if variants else {"status": "missing", "reason": "local_source_not_found"}
        verified, _, _, saved, dimensions, thumbnail = max(choices, key=lambda item: item[:3])
        return dict(saved, dimensions=list(dimensions), variants=variants,
                    status="archived" if verified else "thumbnail_only" if thumbnail else "unverified",
                    quality="thumbnail" if thumbnail else "available_image_unknown_resolution",
                    resolution="metadata_md5" if verified else "database_path_or_md5_filename_only")

    def _file_candidates(self, message, attachment, md5):
        local = attachment.get("local_path")
        if local:
            path = Path(local)
            if not path.is_absolute() and self.media:
                path = self.media / path
            if not self._allowed(path):
                raise ValueError("attachment_path_outside_allowed_roots")
            if path.is_file():
                if md5:
                    with path.open("rb") as stream:
                        if hashlib.file_digest(stream, "md5").hexdigest() != md5:
                            raise ValueError("attachment_md5_mismatch")
                return [path], "database_path"
        name = str(attachment.get("name") or "")
        expected = attachment.get("size_bytes")
        if self.platform == "wechat" and md5 and self.source.get("resource_db"):
            hardlink = Path(self.source["resource_db"]).parent / "hardlink.db"
            try:
                rows = self._query(hardlink, "SELECT file_name,file_size FROM file_hardlink_info_v4 WHERE md5=?", (md5,))
                descriptions = {(r[0], r[1]) for r in rows}
                if len(descriptions) == 1:
                    name, expected = descriptions.pop()
            except sqlite3.Error:
                pass
        if not self.media or not name or Path(name).name != name:
            return [], "filename_missing"
        root = self.media / "msg" / "file" if self.platform == "wechat" else self.media
        candidates = list(dict.fromkeys(p for folder in [root, *self.extra_roots]
                                       for p in self._index(folder).get(name.casefold(), []) if self._allowed(p)))
        if expected is not None:
            candidates = [p for p in candidates if p.stat().st_size == int(expected)]
        if candidates and md5:
            matches = []
            for p in candidates:
                with p.open("rb") as stream:
                    if hashlib.file_digest(stream, "md5").hexdigest() == md5:
                        matches.append(p)
            candidates = matches[:1]
        return candidates, "metadata_md5" if md5 else "unique_filename_and_size_unverified"

    def enrich(self, message):
        if not self.enabled:
            return message
        if self.intensity == "metadata":
            return dict(message, attachments=[dict(a, status="deferred", reason="processing_intensity_metadata")
                                              for a in message.get("attachments", [])])
        output = dict(message)
        output["attachments"] = []
        for original in message.get("attachments", []):
            attachment = dict(original)
            try:
                md5 = self._md5(message, attachment)
                if md5:
                    if attachment.get('md5') and attachment['md5'].casefold() != md5.casefold():
                        # WCDB's resource locator may differ from the original message content hash.
                        attachment['resource_md5'] = md5
                    elif not attachment.get('md5'):
                        attachment['md5'] = md5
                if self.platform == "wechat" and attachment.get("type") == "image":
                    attachment.update(self._image(message, attachment, md5))
                elif self.platform == "qq" and attachment.get("type") == "image":
                    attachment.update(self._qq_image(message, attachment, md5))
                elif attachment.get("type") in {"file", "image", "video", "voice", "audio", "sticker"}:
                    candidates, resolution = self._file_candidates(message, attachment, md5)
                    if len(candidates) != 1:
                        attachment.update(status="missing", reason="ambiguous_local_candidates" if candidates else "local_source_not_found")
                    else:
                        path = candidates[0]
                        attachment.update(self._publish(path=path, suffix=path.suffix), status="archived",
                                          original_source=str(path), resolution=resolution,
                                          message_match_verified=resolution in {"database_path", "metadata_md5"})
                        if attachment.get("type") == "image":
                            try:
                                _, dimensions, _ = _image_info(path.read_bytes())
                                attachment.update(dimensions=list(dimensions), quality="available_image_unknown_resolution")
                                if any("thumb" in part.casefold() for part in path.parts):
                                    attachment.update(status="thumbnail_only", quality="thumbnail")
                            except (OSError, ValueError, SyntaxError):
                                attachment.update(status="unsupported", reason="image_bytes_preserved_but_not_decodable")
                else:
                    attachment.update(status="unsupported", reason="unsupported_attachment_type")
            except (OSError, ValueError, sqlite3.Error, RuntimeError, KeyError) as error:
                attachment.update(status="missing", reason=str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__)
            output["attachments"].append(attachment)
        return output
