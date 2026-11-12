"""Selected existing WeChat media routines; provenance in ASSET_UPSTREAM.md."""
import ctypes
import logging
import os
import re
import struct
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
logger = logging.getLogger(__name__)


def _detect_image_media_type(data: bytes) -> str:
    if not data:
        return "application/octet-stream"

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff") and len(data) >= 4:
        marker = data[3]
        # Most JPEG marker types are in 0xC0..0xFE (APP, SOF, DQT, DHT, SOS, COM, etc.).
        # This avoids false positives where random bytes start with 0xFFD8FF.
        if marker not in (0x00, 0xFF) and marker >= 0xC0:
            return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


class _WxAMConfig(ctypes.Structure):
    _fields_ = [
        ("mode", ctypes.c_int),
        ("reserved", ctypes.c_int),
        # The native export copies 32 bytes from its fifth argument.
        ("options", ctypes.c_ubyte * 24),
    ]


@lru_cache(maxsize=1)
def _get_wxam_decoder(dll_path):
    if os.name != "nt":
        return None
    dll_path = Path(dll_path)
    if not dll_path.exists():
        logger.warning(f"WxAM decoder DLL not found: {dll_path}")
        return None
    try:
        voip_engine = ctypes.WinDLL(str(dll_path))
        fn = voip_engine.wxam_dec_wxam2pic_5
        fn.argtypes = [
            ctypes.c_int64,
            ctypes.c_int,
            ctypes.c_int64,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_int64,
        ]
        fn.restype = ctypes.c_int64
        logger.info(f"WxAM decoder loaded: {dll_path}")
        return fn
    except Exception as e:
        logger.warning(f"Failed to load WxAM decoder DLL: {dll_path} ({e})")
        return None


def _wxgf_to_image_bytes(data: bytes, dll_path: str) -> Optional[bytes]:
    if not data or not data.startswith(b"wxgf"):
        return None
    fn = _get_wxam_decoder(dll_path)
    if fn is None:
        return None

    max_output_size = 52 * 1024 * 1024
    for mode in (0, 3):
        try:
            config = _WxAMConfig()
            config.mode = int(mode)
            config.reserved = 0

            input_buffer = ctypes.create_string_buffer(data, len(data))
            output_buffer = ctypes.create_string_buffer(max_output_size)
            output_size = ctypes.c_int(max_output_size)

            result = fn(
                ctypes.addressof(input_buffer),
                int(len(data)),
                ctypes.addressof(output_buffer),
                ctypes.byref(output_size),
                ctypes.addressof(config),
            )
            if result != 0 or not 0 < output_size.value <= max_output_size:
                continue
            out = output_buffer.raw[: int(output_size.value)]
            if _detect_image_media_type(out[:32]) != "application/octet-stream":
                return out
        except Exception:
            continue
    return None


@lru_cache(maxsize=256)
def _xor_table(key: int) -> bytes:
    return bytes(b ^ key for b in range(256))


def _xor_bytes(data: bytes, key: int) -> bytes:
    """单字节 XOR：查表交给 bytes.translate，避免对整个文件逐字节跑 Python 循环。"""
    return bytes(data).translate(_xor_table(key))


def _decrypt_wechat_dat_v3(data: bytes, xor_key: int) -> bytes:
    return _xor_bytes(data, xor_key)


def _decrypt_wechat_dat_v4(data: bytes, xor_key: int, aes_key: bytes) -> bytes:
    from Crypto.Cipher import AES
    from Crypto.Util import Padding

    header, rest = data[:0xF], data[0xF:]
    signature, aes_size, xor_size = struct.unpack("<6sLLx", header)
    aes_size += AES.block_size - aes_size % AES.block_size

    aes_data = rest[:aes_size]
    raw_data = rest[aes_size:]

    cipher = AES.new(aes_key[:16], AES.MODE_ECB)
    decrypted_data = Padding.unpad(cipher.decrypt(aes_data), AES.block_size)

    if xor_size > 0:
        raw_data = rest[aes_size:-xor_size]
        xor_data = rest[-xor_size:]
        xored_data = _xor_bytes(xor_data, xor_key)
    else:
        xored_data = b""

    return decrypted_data + raw_data + xored_data


def _extract_md5_from_blob(blob: Any) -> str:
    if blob is None:
        return ""
    if isinstance(blob, memoryview):
        data = bytes(blob)
    elif isinstance(blob, (bytes, bytearray)):
        data = bytes(blob)
    else:
        try:
            data = bytes(blob)
        except Exception:
            return ""

    if not data:
        return ""

    # Prefer md5 that appears as an actual `.dat` filename (incl. _t.dat/.t.dat variants).
    # This matches echotrace's idea: packed_info often contains multiple 32-hex tokens, but only
    # the one referenced by a file path is the correct on-disk basename.
    try:
        m2 = _DAT_MD5_RE.findall(data)
    except Exception:
        m2 = []
    if m2:
        best2 = Counter([x.lower() for x in m2]).most_common(1)[0][0]
        try:
            return best2.decode("ascii", errors="ignore")
        except Exception:
            return ""

    m = _MD5_HEX_RE.findall(data)
    if not m:
        return ""
    best = Counter([x.lower() for x in m]).most_common(1)[0][0]
    try:
        return best.decode("ascii", errors="ignore")
    except Exception:
        return ""


def _extract_md5_from_packed_info(packed_info: Any) -> str:
    if packed_info is None:
        return ""

    data: bytes = b""
    if isinstance(packed_info, memoryview):
        data = packed_info.tobytes()
    elif isinstance(packed_info, (bytes, bytearray)):
        data = bytes(packed_info)
    elif isinstance(packed_info, str):
        s = packed_info.strip()
        if s.lower().startswith("0x"):
            s = s[2:]
        if s and _PACKED_INFO_HEX_RE.fullmatch(s) and (len(s) % 2 == 0):
            try:
                data = bytes.fromhex(s)
            except Exception:
                data = b""
        else:
            data = s.encode("utf-8", errors="ignore")
    else:
        if isinstance(packed_info, (int, float, bool)):
            data = b""
        else:
            try:
                data = bytes(packed_info)
            except Exception:
                data = b""

    if len(data) == 16:
        return data.hex()

    md5 = _extract_md5_from_blob(data)
    md5 = str(md5 or "").strip().lower()
    if len(md5) == 32 and all(c in "0123456789abcdef" for c in md5):
        return md5
    return ""

_MD5_HEX_RE = re.compile(rb"(?i)[0-9a-f]{32}")
_DAT_MD5_RE = re.compile(rb"(?i)([0-9a-f]{32})(?:[._][thbc])?\.dat")
_PACKED_INFO_HEX_RE = re.compile(r"(?i)^[0-9a-f]+$")
