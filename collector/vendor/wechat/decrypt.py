"""Selected upstream offline database decryption core; see UPSTREAM.md."""


import hashlib


import hmac


import os


import json


import struct


import time


import tempfile


from pathlib import Path


from typing import Any


from cryptography.hazmat.backends import default_backend


from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


from .sqlite_diagnostics import collect_sqlite_diagnostics, sqlite_diagnostics_status, repair_sqlite_indexes


from .sqlite_wal import merge_wal_snapshot


SQLITE_HEADER = b"SQLite format 3\x00"


PAGE_SIZE = 4096


KEY_SIZE = 32


SALT_SIZE = 16


IV_SIZE = 16


HMAC_SIZE = 64


RESERVE_SIZE = IV_SIZE + HMAC_SIZE


def _derive_mac_key(enc_key: bytes, salt: bytes) -> bytes:
    """Derive SQLCipher/WCDB page HMAC key."""
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SIZE)


def _derive_sqlcipher_enc_key(key_material: bytes, salt: bytes) -> bytes:
    """Derive AES enc_key from SQLCipher passphrase/base key."""
    return hashlib.pbkdf2_hmac("sha512", key_material, salt, 256000, dklen=KEY_SIZE)


def _compute_page_hmac(mac_key: bytes, page: bytes, page_num: int) -> bytes:
    offset = SALT_SIZE if page_num == 1 else 0
    data_end = PAGE_SIZE - RESERVE_SIZE + IV_SIZE
    mac = hmac.new(mac_key, digestmod=hashlib.sha512)
    mac.update(page[offset:data_end])
    mac.update(page_num.to_bytes(4, "little"))
    return mac.digest()


def _compute_page_hmac_variant(
    mac_key: bytes,
    page: bytes,
    page_num: int,
    *,
    endian: str = "little",
    include_iv: bool = True,
) -> bytes:
    """用于诊断的 HMAC 变体计算，不参与实际解密决策。"""
    offset = SALT_SIZE if page_num == 1 else 0
    data_end = PAGE_SIZE - RESERVE_SIZE + (IV_SIZE if include_iv else 0)
    mac = hmac.new(mac_key, digestmod=hashlib.sha512)
    mac.update(page[offset:data_end])
    mac.update(page_num.to_bytes(4, endian))
    return mac.digest()


def _hash_prefix(data: bytes, *, length: int = 16) -> str:
    """返回 SHA256 前缀，避免日志输出明文数据。"""
    try:
        return hashlib.sha256(bytes(data or b"")).hexdigest()[: max(int(length), 8)]
    except Exception:
        return ""


def _hex_prefix(data: bytes, *, length: int = 32) -> str:
    try:
        return bytes(data or b"")[: max(int(length), 0)].hex()
    except Exception:
        return ""


def _safe_file_snapshot(path: str | Path) -> dict[str, Any]:
    """采集源/输出文件与 WAL 旁路文件信息，用于定位解密时文件是否变化。"""
    p = Path(path)
    out: dict[str, Any] = {"path": str(p), "exists": False}
    try:
        st = p.stat()
        out.update(
            {
                "exists": True,
                "size": int(st.st_size),
                "inode": int(st.st_ino),
                "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
            }
        )
    except Exception as exc:
        out["stat_error"] = f"{type(exc).__name__}: {' '.join(str(exc).split())[:180]}"

    siblings: dict[str, Any] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        sp = Path(str(p) + suffix)
        try:
            st = sp.stat()
            siblings[suffix] = {
                "exists": True,
                "size": int(st.st_size),
                "inode": int(st.st_ino),
                "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
            }
        except FileNotFoundError:
            siblings[suffix] = {"exists": False}
        except Exception as exc:
            siblings[suffix] = {
                "exists": False,
                "stat_error": f"{type(exc).__name__}: {' '.join(str(exc).split())[:180]}",
            }
    out["siblings"] = siblings
    return out


def _read_plain_sqlite_header_debug(path: str | Path) -> dict[str, Any]:
    """解析明文 SQLite 头部关键字段，帮助定位输出库结构问题。"""
    p = Path(path)
    out: dict[str, Any] = {"path": str(p)}
    try:
        with p.open("rb") as f:
            header = f.read(100)
        out["header_len"] = len(header)
        out["header_ok"] = header.startswith(SQLITE_HEADER)
        out["header_hex"] = header[:32].hex()
        if len(header) >= 100:
            raw_page_size = struct.unpack(">H", header[16:18])[0]
            out.update(
                {
                    "page_size_header": 65536 if raw_page_size == 1 else int(raw_page_size),
                    "write_version": int(header[18]),
                    "read_version": int(header[19]),
                    "reserved_space": int(header[20]),
                    "max_payload_fraction": int(header[21]),
                    "min_payload_fraction": int(header[22]),
                    "leaf_payload_fraction": int(header[23]),
                    "file_change_counter": int.from_bytes(header[24:28], "big"),
                    "db_size_pages_header": int.from_bytes(header[28:32], "big"),
                    "freelist_trunk_page": int.from_bytes(header[32:36], "big"),
                    "freelist_pages": int.from_bytes(header[36:40], "big"),
                    "schema_cookie": int.from_bytes(header[40:44], "big"),
                    "schema_format": int.from_bytes(header[44:48], "big"),
                    "text_encoding": int.from_bytes(header[56:60], "big"),
                }
            )
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {' '.join(str(exc).split())[:180]}"
    return out


def _plain_page_btree_debug(page_plain: bytes, page_num: int) -> dict[str, Any]:
    """解析明文页 B-tree 页头摘要，不输出任何业务明文。"""
    out: dict[str, Any] = {"page": int(page_num), "plain_sha256": _hash_prefix(page_plain, length=24)}
    try:
        hdr = 100 if int(page_num) == 1 else 0
        if len(page_plain) >= hdr + 12:
            page_type = int(page_plain[hdr])
            out["btree_header_offset"] = int(hdr)
            out["btree_page_type"] = page_type
            out["btree_page_type_name"] = {
                2: "interior_index",
                5: "interior_table",
                10: "leaf_index",
                13: "leaf_table",
            }.get(page_type, "unknown")
            out["first_freeblock"] = int.from_bytes(page_plain[hdr + 1 : hdr + 3], "big")
            out["cell_count"] = int.from_bytes(page_plain[hdr + 3 : hdr + 5], "big")
            out["cell_content_area"] = int.from_bytes(page_plain[hdr + 5 : hdr + 7], "big")
            out["fragmented_free_bytes"] = int(page_plain[hdr + 7])
            if page_type in (2, 5):
                out["right_most_pointer"] = int.from_bytes(page_plain[hdr + 8 : hdr + 12], "big")
    except Exception as exc:
        out["btree_parse_error"] = f"{type(exc).__name__}: {' '.join(str(exc).split())[:160]}"
    return out


def _build_page_anomaly_debug(
    enc_key: bytes,
    mac_key: bytes,
    page: bytes,
    page_num: int,
    *,
    stored_hmac: bytes | None = None,
    expected_hmac: bytes | None = None,
    reason: str = "hmac",
) -> dict[str, Any]:
    """构造异常页诊断信息，默认只记录哈希/页头摘要。"""
    page = bytes(page or b"")
    stored = stored_hmac if stored_hmac is not None else page[PAGE_SIZE - HMAC_SIZE : PAGE_SIZE]
    expected = expected_hmac if expected_hmac is not None else _compute_page_hmac(mac_key, page, page_num)
    iv = page[PAGE_SIZE - RESERVE_SIZE : PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
    encrypted_payload = page[SALT_SIZE if page_num == 1 else 0 : PAGE_SIZE - RESERVE_SIZE]
    out: dict[str, Any] = {
        "reason": str(reason),
        "page": int(page_num),
        "byte_start": int((int(page_num) - 1) * PAGE_SIZE),
        "byte_end_exclusive": int(int(page_num) * PAGE_SIZE),
        "page_size": int(len(page)),
        "page_sha256": _hash_prefix(page, length=24),
        "encrypted_payload_sha256": _hash_prefix(encrypted_payload, length=24),
        "iv_hex": _hex_prefix(iv, length=16),
        "stored_hmac_prefix": _hex_prefix(stored, length=16),
        "expected_hmac_prefix": _hex_prefix(expected, length=16),
        "hmac_match_current": bool(hmac.compare_digest(stored, expected)),
    }

    variants: dict[str, bool] = {}
    for candidate_page in (page_num - 1, page_num, page_num + 1):
        if candidate_page <= 0:
            continue
        for endian in ("little", "big"):
            for include_iv in (True, False):
                key = f"page={candidate_page};endian={endian};include_iv={int(include_iv)}"
                try:
                    variants[key] = bool(
                        hmac.compare_digest(
                            stored,
                            _compute_page_hmac_variant(
                                mac_key,
                                page,
                                int(candidate_page),
                                endian=endian,
                                include_iv=include_iv,
                            ),
                        )
                    )
                except Exception:
                    variants[key] = False
    out["hmac_variant_matches"] = [k for k, v in variants.items() if v]

    try:
        plain_page = _decrypt_page(enc_key, page, int(page_num))
        out["aes_decrypt_ok"] = True
        out["plain"] = _plain_page_btree_debug(plain_page, int(page_num))
    except Exception as exc:
        out["aes_decrypt_ok"] = False
        out["aes_error"] = f"{type(exc).__name__}: {' '.join(str(exc).split())[:180]}"

    return out


def _resolve_page1_key_material(key_material: bytes, page1: bytes) -> tuple[bytes, bytes, str] | None:
    """Detect whether input key is raw enc_key or SQLCipher passphrase by page-1 HMAC."""
    if len(page1) < PAGE_SIZE:
        return None

    salt = page1[:SALT_SIZE]
    stored_page1_hmac = page1[PAGE_SIZE - HMAC_SIZE: PAGE_SIZE]
    candidates = [
        ("raw_enc_key", key_material, _derive_mac_key(key_material, salt)),
    ]

    derived_key = _derive_sqlcipher_enc_key(key_material, salt)
    candidates.append(("sqlcipher_passphrase", derived_key, _derive_mac_key(derived_key, salt)))

    for mode, enc_key, mac_key in candidates:
        if hmac.compare_digest(stored_page1_hmac, _compute_page_hmac(mac_key, page1, 1)):
            return enc_key, mac_key, mode

    return None


def _decrypt_page(enc_key: bytes, page: bytes, page_num: int) -> bytes:
    iv = page[PAGE_SIZE - RESERVE_SIZE: PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
    offset = SALT_SIZE if page_num == 1 else 0
    encrypted_page = page[offset: PAGE_SIZE - RESERVE_SIZE]

    cipher = Cipher(
        algorithms.AES(enc_key),
        modes.CBC(iv),
        backend=default_backend(),
    )
    decryptor = cipher.decryptor()
    decrypted_page = decryptor.update(encrypted_page) + decryptor.finalize()

    # Plain SQLite pages do not carry SQLCipher/WCDB IV/HMAC reserve bytes.
    # Keep page size stable by zero-filling the reserve tail.
    if page_num == 1:
        return SQLITE_HEADER + decrypted_page + (b"\x00" * RESERVE_SIZE)
    return decrypted_page + (b"\x00" * RESERVE_SIZE)


def _build_decrypt_failure_message(result: dict) -> str:
    failed_pages = int(result.get("failed_pages") or 0)
    successful_pages = int(result.get("successful_pages") or 0)
    diagnostic_status = str(result.get("diagnostic_status") or "").strip()
    diagnostics = dict(result.get("diagnostics") or {})

    detail = (
        diagnostics.get("quick_check_error")
        or diagnostics.get("connect_error")
        or diagnostics.get("table_list_error")
        or diagnostics.get("page_count_error")
        or diagnostics.get("quick_check")
        or diagnostic_status
    )
    detail_text = " ".join(str(detail or "").split()).strip()

    if failed_pages > 0 and successful_pages == 0:
        if detail_text:
            return f"数据库校验未通过，密钥可能不匹配当前账号: {detail_text}"
        return "数据库校验未通过，密钥可能不匹配当前账号"

    if diagnostic_status and diagnostic_status != "ok":
        if detail_text:
            return f"解密输出不是有效的 SQLite 数据库: {detail_text}"
        return "解密输出不是有效的 SQLite 数据库"

    if failed_pages > 0:
        return "解密输出包含页失败，结果不完整"

    return ""


class WeChatDatabaseDecryptor:
    """微信4.x数据库解密器"""

    def __init__(self, key_hex: str):
        """初始化解密器

        参数:
            key_hex: 64位十六进制密钥
        """
        if len(key_hex) != 64:
            raise ValueError("密钥必须是64位十六进制字符串")
        
        try:
            self.key_bytes = bytes.fromhex(key_hex)
        except ValueError:
            raise ValueError("密钥必须是有效的十六进制字符串")
        self.last_result: dict = {}
    
    def decrypt_database(self, db_path: str, output_path: str) -> bool:
        """解密微信4.x版本数据库

        使用SQLCipher 4.0参数:
        - PBKDF2-SHA512, 256000轮迭代
        - AES-256-CBC加密
        - HMAC-SHA512验证
        - 页面大小4096字节
        """
        from logging import getLogger
        logger = getLogger(__name__)

        result = {
            "db_path": str(db_path),
            "db_name": Path(str(db_path)).name,
            "output_path": str(output_path),
            "success": False,
            "copied_as_sqlite": False,
            "input_size": 0,
            "output_size": 0,
            "total_pages": 0,
            "successful_pages": 0,
            "failed_pages": 0,
            "failed_page_samples": [],
            "failure_reasons": {},
            "hmac_warning_pages": 0,
            "hmac_warning_samples": [],
            "hmac_debug_samples": [],
            "aes_debug_samples": [],
            "source_snapshot_before": {},
            "source_snapshot_after": {},
            "source_changed_during_read": False,
            "read_ms": 0,
            "key_mode": "",
            "key_authenticated": False,
            "wal": {},
            "index_repair": {},
            "input_layout": {},
            "expected_output_size": 0,
            "output_header_debug": {},
            "diagnostics": {},
            "diagnostic_status": "not_run",
            "error": "",
        }
        self.last_result = result
        working_output: Path | None = None

        def _write_output(data: bytes) -> None:
            nonlocal working_output
            with tempfile.NamedTemporaryFile(dir=Path(output_path).parent, prefix=".decrypt-", suffix=".db", delete=False) as stream:
                working_output = Path(stream.name)
                # A materialized snapshot is standalone; do not leave WAL mode
                # enabled and let readers create new sidecars beside the export.
                if data.startswith(SQLITE_HEADER) and data[18:20] == b"\x02\x02":
                    stream.write(memoryview(data)[:18])
                    stream.write(b"\x01\x01")
                    stream.write(memoryview(data)[20:])
                else:
                    stream.write(data)

        def _append_failed_page(page_num: int, reason: str, error: str = "") -> None:
            result["failure_reasons"][reason] = int(result["failure_reasons"].get(reason) or 0) + 1
            if len(result["failed_page_samples"]) >= 8:
                return
            item = {"page": int(page_num), "reason": str(reason)}
            err = " ".join(str(error or "").split()).strip()
            if err:
                item["error"] = err[:200]
            result["failed_page_samples"].append(item)

        def _append_hmac_warning_page(page_num: int) -> None:
            # 非首页 HMAC 异常不再直接丢弃页面：部分微信 4.x 大库在 1GiB 边界会出现
            # 单页 HMAC 不匹配，但页面本身仍可正常解密。丢页会导致后续页号整体错位。
            result["hmac_warning_pages"] = int(result.get("hmac_warning_pages") or 0) + 1
            if len(result["hmac_warning_samples"]) >= 8:
                return
            result["hmac_warning_samples"].append({"page": int(page_num), "reason": "hmac"})

        def _finalize(success: bool, error: str = "") -> bool:
            normalized_success = bool(success)
            result["success"] = normalized_success
            if error:
                result["error"] = " ".join(str(error).split()).strip()

            output_file = working_output
            if output_file is not None and output_file.exists():
                try:
                    result["output_size"] = int(output_file.stat().st_size)
                except Exception:
                    pass

                diagnostics = collect_sqlite_diagnostics(output_file, quick_check=True)
                if (normalized_success and diagnostics.get("quick_check_ok") is False
                        and result["key_authenticated"] and result["failed_pages"] == 0
                        and result["hmac_warning_pages"] == 0):
                    result["index_repair"] = repair_sqlite_indexes(output_file)
                    if result["index_repair"].get("success"):
                        diagnostics = collect_sqlite_diagnostics(output_file, quick_check=True)
                result["output_size"] = output_file.stat().st_size
                diagnostics["path"] = str(output_path)
                result["diagnostics"] = diagnostics
                result["diagnostic_status"] = sqlite_diagnostics_status(diagnostics)
                result["output_header_debug"] = _read_plain_sqlite_header_debug(output_file)
                result["output_header_debug"]["path"] = str(output_path)

            if normalized_success:
                failure_message = _build_decrypt_failure_message(result)
                if failure_message:
                    normalized_success = False
                    result["success"] = False
                    if not result["error"]:
                        result["error"] = failure_message
                    if output_file is not None and output_file.exists():
                        try:
                            output_file.unlink()
                        except Exception as exc:
                            logger.warning("删除无效解密输出失败: %s, 错误: %s", output_file, exc)

            if normalized_success and output_file is not None:
                # Never let an old output WAL override a freshly decrypted main file.
                if any(Path(str(output_path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                    raise ValueError("输出数据库正在使用或遗留日志文件，请关闭读取连接后重试")
                os.replace(output_file, output_path)
            payload = {
                "key_authenticated": result["key_authenticated"],
                "wal": result["wal"],
                "index_repair": result["index_repair"],
                "db_name": result["db_name"],
                "db_path": result["db_path"],
                "output_path": result["output_path"],
                "success": result["success"],
                "copied_as_sqlite": result["copied_as_sqlite"],
                "input_size": result["input_size"],
                "output_size": result["output_size"],
                "total_pages": result["total_pages"],
                "successful_pages": result["successful_pages"],
                "failed_pages": result["failed_pages"],
                "failure_reasons": result["failure_reasons"],
                "failed_page_samples": result["failed_page_samples"],
                "hmac_warning_pages": result["hmac_warning_pages"],
                "hmac_warning_samples": result["hmac_warning_samples"],
                "hmac_debug_samples": result["hmac_debug_samples"],
                "aes_debug_samples": result["aes_debug_samples"],
                "source_snapshot_before": result["source_snapshot_before"],
                "source_snapshot_after": result["source_snapshot_after"],
                "source_changed_during_read": result["source_changed_during_read"],
                "read_ms": result["read_ms"],
                "key_mode": result["key_mode"],
                "input_layout": result["input_layout"],
                "expected_output_size": result["expected_output_size"],
                "output_header_debug": result["output_header_debug"],
                "diagnostic_status": result["diagnostic_status"],
                "diagnostics": result["diagnostics"],
                "error": result["error"],
            }
            log_fn = logger.info
            if (
                (not result["success"])
                or int(result["failed_pages"] or 0) > 0
                or int(result.get("hmac_warning_pages") or 0) > 0
                or str(result["diagnostic_status"] or "") != "ok"
            ):
                log_fn = logger.warning
            log_fn("[decrypt.diagnostic] %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))
            self.last_result = result
            return bool(result["success"])

        logger.info(f"开始解密数据库: {db_path}")
        
        try:
            if Path(db_path).resolve() == Path(output_path).resolve() or (
                Path(output_path).exists() and os.path.samefile(db_path, output_path)
            ):
                raise ValueError("解密输出不能覆盖源数据库")
            source_snapshot_before = _safe_file_snapshot(db_path)
            result["source_snapshot_before"] = source_snapshot_before
            logger.info(
                "[decrypt.pipeline] source_snapshot_before %s",
                json.dumps(
                    {
                        "db_name": result["db_name"],
                        "snapshot": source_snapshot_before,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )

            read_t0 = time.perf_counter()
            with open(db_path, 'rb') as f:
                encrypted_data = f.read()
            wal_path = Path(str(db_path) + "-wal")
            try:
                wal_data = wal_path.read_bytes()
            except FileNotFoundError:
                wal_data = b""
            journal_path = Path(str(db_path) + "-journal")
            try:
                with journal_path.open('rb') as journal:
                    if journal.read(8) == bytes.fromhex('d9d505f920a163d7'):
                        raise ValueError("源数据库存在待恢复的回滚日志，请先通过微信完成恢复")
            except FileNotFoundError:
                pass
            result["read_ms"] = round((time.perf_counter() - read_t0) * 1000.0, 1)

            source_snapshot_after = _safe_file_snapshot(db_path)
            result["source_snapshot_after"] = source_snapshot_after
            before_size = int(source_snapshot_before.get("size") or 0)
            after_size = int(source_snapshot_after.get("size") or 0)
            before_mtime = int(source_snapshot_before.get("mtime_ns") or 0)
            after_mtime = int(source_snapshot_after.get("mtime_ns") or 0)
            source_changed = bool(
                before_size != after_size or before_mtime != after_mtime
                or source_snapshot_before.get("inode") != source_snapshot_after.get("inode")
                or any(source_snapshot_before.get("siblings", {}).get(suffix) !=
                       source_snapshot_after.get("siblings", {}).get(suffix)
                       for suffix in ("-wal", "-journal"))
            )
            result["source_changed_during_read"] = source_changed
            logger.info(
                "[decrypt.pipeline] source_snapshot_after %s",
                json.dumps(
                    {
                        "db_name": result["db_name"],
                        "snapshot": source_snapshot_after,
                        "read_ms": result["read_ms"],
                        "source_changed_during_read": source_changed,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            if source_changed:
                logger.warning(
                    "[decrypt.pipeline] source_changed_during_read db=%s before_size=%s after_size=%s before_mtime_ns=%s after_mtime_ns=%s",
                    result["db_name"],
                    before_size,
                    after_size,
                    before_mtime,
                    after_mtime,
                )

            if source_changed:
                return _finalize(False, "源数据库或 WAL 在读取期间发生变化，请退出微信后重试")

            logger.info(f"读取文件大小: {len(encrypted_data)} bytes")
            result["input_size"] = int(len(encrypted_data))
            result["input_layout"] = {
                "page_size": PAGE_SIZE,
                "reserve_size": RESERVE_SIZE,
                "iv_size": IV_SIZE,
                "hmac_size": HMAC_SIZE,
                "input_size": int(len(encrypted_data)),
                "input_size_mod_page": int(len(encrypted_data) % PAGE_SIZE),
                "total_pages_floor": int(len(encrypted_data) // PAGE_SIZE),
                "total_pages_ceil": int((len(encrypted_data) + PAGE_SIZE - 1) // PAGE_SIZE),
                "starts_with_sqlite_header": bool(encrypted_data.startswith(SQLITE_HEADER)),
                "first16_hex": encrypted_data[:16].hex(),
            }
            logger.info(
                "[decrypt.pipeline] input_layout %s",
                json.dumps(
                    {
                        "db_name": result["db_name"],
                        "input_layout": result["input_layout"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )

            if len(encrypted_data) < 4096:
                logger.warning(f"文件太小，跳过解密: {db_path}")
                return _finalize(False, "file_too_small")

            # 检查是否已经是解密的数据库
            if encrypted_data.startswith(SQLITE_HEADER):
                logger.info(f"文件已是SQLite格式，直接复制: {db_path}")
                plain_page_size = int.from_bytes(encrypted_data[16:18], 'big')
                if plain_page_size == 1:
                    plain_page_size = 65536
                if plain_page_size < 512 or plain_page_size > 65536 or plain_page_size & (plain_page_size - 1):
                    raise ValueError("SQLite 页大小无效")
                encrypted_data, result["wal"] = merge_wal_snapshot(encrypted_data, wal_data, plain_page_size)
                if result["wal"]["committed_frames"]:
                    merged = bytearray(encrypted_data)
                    merged[28:32] = result["wal"]["database_pages"].to_bytes(4, "big")
                    encrypted_data = bytes(merged)
                _write_output(encrypted_data)
                result["copied_as_sqlite"] = True
                return _finalize(True)
            
            page1 = encrypted_data[:PAGE_SIZE]
            resolved_key_material = _resolve_page1_key_material(self.key_bytes, page1)
            if resolved_key_material is None:
                _append_failed_page(1, "hmac")
                result["total_pages"] = int(len(encrypted_data) // PAGE_SIZE)
                result["failed_pages"] = 1
                logger.warning("Page 1 HMAC verification failed; key does not match database: %s", db_path)
                return _finalize(False, "key_mismatch")

            enc_key, mac_key, key_mode = resolved_key_material
            result["key_mode"] = key_mode
            result["key_authenticated"] = True
            logger.info("Page 1 HMAC verification passed: mode=%s path=%s", key_mode, db_path)
            logger.info(
                "[decrypt.pipeline] key_material_resolved %s",
                json.dumps(
                    {
                        "db_name": result["db_name"],
                        "key_mode": key_mode,
                        "salt_sha256": _hash_prefix(page1[:SALT_SIZE], length=24),
                        "page1_stored_hmac_prefix": _hex_prefix(page1[PAGE_SIZE - HMAC_SIZE : PAGE_SIZE], length=16),
                        "page1_expected_hmac_prefix": _hex_prefix(_compute_page_hmac(mac_key, page1, 1), length=16),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )

            encrypted_data, result["wal"] = merge_wal_snapshot(
                encrypted_data, wal_data, PAGE_SIZE,
                verify_page=lambda page, pgno: hmac.compare_digest(
                    page[-HMAC_SIZE:], _compute_page_hmac(mac_key, page, pgno)),
            )
            decrypted_data = bytearray()
            total_pages = (len(encrypted_data) + PAGE_SIZE - 1) // PAGE_SIZE
            successful_pages = 0
            failed_pages = 0
            result["total_pages"] = int(total_pages)
            result["expected_output_size"] = int(total_pages * PAGE_SIZE)
            logger.info(
                "[decrypt.pipeline] page_loop_start db=%s total_pages=%s expected_output_size=%s",
                result["db_name"],
                int(total_pages),
                int(result["expected_output_size"]),
            )

            for cur_page in range(total_pages):
                page_num = cur_page + 1
                start = cur_page * PAGE_SIZE
                page = encrypted_data[start:start + PAGE_SIZE]
                if not page:
                    break
                if len(page) < PAGE_SIZE:
                    logger.warning(
                        "Page %s is short: %s bytes; padding to %s bytes",
                        page_num,
                        len(page),
                        PAGE_SIZE,
                    )
                    page = page + (b"\x00" * (PAGE_SIZE - len(page)))

                stored_hmac = page[PAGE_SIZE - HMAC_SIZE: PAGE_SIZE]
                expected_hmac = _compute_page_hmac(mac_key, page, page_num)
                if not hmac.compare_digest(stored_hmac, expected_hmac):
                    _append_hmac_warning_page(page_num)
                    # Keep every warning count, but expensive diagnostic variants need only samples.
                    if len(result["hmac_debug_samples"]) < 8:
                        logger.warning("Page %s HMAC verification failed; decrypting page anyway", page_num)
                        anomaly_debug = _build_page_anomaly_debug(
                            enc_key,
                            mac_key,
                            page,
                            page_num,
                            stored_hmac=stored_hmac,
                            expected_hmac=expected_hmac,
                            reason="hmac",
                        )
                        result["hmac_debug_samples"].append(anomaly_debug)
                        logger.warning(
                            "[decrypt.page_anomaly] %s",
                            json.dumps(
                                {"db_name": result["db_name"], "anomaly": anomaly_debug},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        )

                try:
                    decrypted_data.extend(_decrypt_page(enc_key, page, page_num))
                    successful_pages += 1
                except Exception as e:
                    logger.error("Page %s AES decryption failed: %s", page_num, e)
                    failed_pages += 1
                    _append_failed_page(page_num, "aes", str(e))
                    aes_debug = _build_page_anomaly_debug(
                        enc_key,
                        mac_key,
                        page,
                        page_num,
                        stored_hmac=stored_hmac,
                        expected_hmac=expected_hmac,
                        reason="aes",
                    )
                    if len(result["aes_debug_samples"]) < 8:
                        result["aes_debug_samples"].append(aes_debug)
                    logger.error(
                        "[decrypt.page_anomaly] %s",
                        json.dumps(
                            {
                                "db_name": result["db_name"],
                                "anomaly": aes_debug,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                    # 保留页占位，避免后续页整体错位导致 SQLite 必然损坏。
                    decrypted_data.extend(b"\x00" * PAGE_SIZE)
                    continue

                if total_pages >= 100000 and page_num % 50000 == 0:
                    logger.info(
                        "[decrypt.pipeline] page_loop_progress db=%s page=%s/%s successful_pages=%s failed_pages=%s hmac_warning_pages=%s output_bytes=%s",
                        result["db_name"],
                        int(page_num),
                        int(total_pages),
                        int(successful_pages),
                        int(failed_pages),
                        int(result.get("hmac_warning_pages") or 0),
                        int(len(decrypted_data)),
                    )

            result["successful_pages"] = int(successful_pages)
            result["failed_pages"] = int(failed_pages)

            # WAL commit size is authoritative even if page 1 was not in the WAL.
            if result["wal"]["committed_frames"]:
                decrypted_data[28:32] = result["wal"]["database_pages"].to_bytes(4, "big")
            # 写入待校验的临时文件，验证成功后原子替换输出。
            _write_output(decrypted_data)

            logger.info(f"解密文件大小: {len(decrypted_data)} bytes")
            if int(len(decrypted_data)) != int(result["expected_output_size"]):
                logger.warning(
                    "[decrypt.pipeline] output_size_mismatch db=%s output_size=%s expected_output_size=%s delta=%s",
                    result["db_name"],
                    int(len(decrypted_data)),
                    int(result["expected_output_size"]),
                    int(len(decrypted_data)) - int(result["expected_output_size"]),
                )
            if failed_pages > 0:
                logger.warning(
                    "解密输出包含页失败: db=%s total_pages=%s failed_pages=%s failure_reasons=%s samples=%s",
                    result["db_name"],
                    int(total_pages),
                    int(failed_pages),
                    json.dumps(result["failure_reasons"], ensure_ascii=False, sort_keys=True),
                    json.dumps(result["failed_page_samples"], ensure_ascii=False),
                )
            if int(result.get("hmac_warning_pages") or 0) > 0:
                logger.warning(
                    "解密输出包含HMAC告警页但已保留页内容: db=%s total_pages=%s hmac_warning_pages=%s samples=%s",
                    result["db_name"],
                    int(total_pages),
                    int(result.get("hmac_warning_pages") or 0),
                    json.dumps(result["hmac_warning_samples"], ensure_ascii=False),
                )
            return _finalize(True)

        except Exception as e:
            logger.error(f"解密失败: {db_path}, 错误: {e}")
            return _finalize(False, str(e))
        finally:
            if working_output is not None:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    try:
                        Path(str(working_output) + suffix).unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning("清理解密临时文件失败: %s", exc)
