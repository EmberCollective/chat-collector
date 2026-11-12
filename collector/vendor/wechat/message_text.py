"""Selected upstream SQLite/zstd text decoder; see UPSTREAM.md."""
import base64
import html
import re
from typing import Any, Optional
import zstandard as zstd

MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class MessageDecodeError(ValueError):
    pass


def _decompress_limited(data):
    if not data.startswith(b"\x28\xb5\x2f\xfd"):
        raise zstd.ZstdError("Not a zstd message")
    try:
        size = zstd.frame_content_size(data)
        if size not in (zstd.CONTENTSIZE_UNKNOWN, zstd.CONTENTSIZE_ERROR) and size > MAX_MESSAGE_BYTES:
            raise MessageDecodeError("WeChat message exceeds the 16 MiB decoding limit")
        return zstd.ZstdDecompressor(max_window_size=16 * 1024).decompress(data, max_output_size=MAX_MESSAGE_BYTES)
    except zstd.ZstdError as error:
        raise MessageDecodeError("Invalid or over-limit WeChat zstd message") from error

def _decode_sqlite_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    if isinstance(value, memoryview):
        try:
            return bytes(value).decode("utf-8", errors="ignore")
        except Exception:
            return ""
    return str(value)


def _is_mostly_printable_text(s: str) -> bool:
    if not s:
        return False
    sample = s[:600]
    if not sample:
        return False
    printable = sum(1 for ch in sample if ch.isprintable() or ch in {"\n", "\r", "\t"})
    return (printable / len(sample)) >= 0.85


def _looks_like_xml(s: str) -> bool:
    if not s:
        return False
    t = s.lstrip()
    if t.startswith('"') and t.endswith('"'):
        t = t.strip('"').lstrip()
    return t.startswith("<")


def _decode_message_content(compress_value: Any, message_value: Any) -> str:
    def try_decode_text_blob(text: str) -> Optional[str]:
        t = (text or "").strip()
        if not t:
            return None

        # zstd frame magic: 28 b5 2f fd
        zstd_magic = b"\x28\xb5\x2f\xfd"

        if len(t) >= 16 and len(t) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", t):
            try:
                raw = bytes.fromhex(t)
                if zstd is not None and raw.startswith(zstd_magic):
                    try:
                        out = _decompress_limited(raw)
                        s2 = out.decode("utf-8", errors="ignore")
                        s2 = html.unescape(s2.strip())
                        if _looks_like_xml(s2) or _is_mostly_printable_text(s2):
                            return s2
                    except MessageDecodeError:
                        raise
                    except Exception:
                        pass
                s2 = raw.decode("utf-8", errors="ignore")
                s2 = html.unescape(s2.strip())
                # Avoid decoding user-sent pure-hex text (e.g. "68656c6c6f") into arbitrary strings;
                # only accept non-zstd hex if it still looks like a message XML payload.
                s2_lower = s2.lower()
                if (
                    _looks_like_xml(s2)
                    or ("<msg" in s2_lower and "</msg>" in s2_lower)
                    or "<appmsg" in s2_lower
                ):
                    return s2
            except MessageDecodeError:
                raise
            except Exception:
                return None

        if len(t) >= 24 and len(t) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/=]+", t):
            try:
                raw = base64.b64decode(t)
                if zstd is not None and raw.startswith(zstd_magic):
                    try:
                        out = _decompress_limited(raw)
                        s2 = out.decode("utf-8", errors="ignore")
                        s2 = html.unescape(s2.strip())
                        if _looks_like_xml(s2) or _is_mostly_printable_text(s2):
                            return s2
                    except MessageDecodeError:
                        raise
                    except Exception:
                        pass
                s2 = raw.decode("utf-8", errors="ignore")
                s2 = html.unescape(s2.strip())
                s2_lower = s2.lower()
                if (
                    _looks_like_xml(s2)
                    or ("<msg" in s2_lower and "</msg>" in s2_lower)
                    or "<appmsg" in s2_lower
                ):
                    return s2
            except MessageDecodeError:
                raise
            except Exception:
                return None

        return None

    msg_text = _decode_sqlite_text(message_value)

    # Realtime WCDB mode can return message_content as a hex/base64 encoded blob string
    # (often a zstd frame starting with 28b52ffd...), while compress_content is null.
    # NOTE: some callers set sqlite3.text_factory=bytes, so TEXT may arrive as bytes even when it
    # is actually hex/base64 text; decode from msg_text, not from the raw python type.
    s = html.unescape(msg_text.strip())
    s2 = try_decode_text_blob(s)
    if s2:
        msg_text = s2

    if isinstance(message_value, (bytes, bytearray, memoryview)):
        raw = bytes(message_value) if isinstance(message_value, memoryview) else message_value
        if raw.startswith(b"\x28\xb5\x2f\xfd") and zstd is not None:
            try:
                out = _decompress_limited(raw)
                s = out.decode("utf-8", errors="ignore")
                s = html.unescape(s.strip())
                if _looks_like_xml(s) or _is_mostly_printable_text(s):
                    msg_text = s
            except MessageDecodeError:
                raise
            except Exception:
                pass

    if compress_value is None:
        return msg_text

    if isinstance(compress_value, str):
        s = html.unescape(compress_value.strip())
        s2 = try_decode_text_blob(s)
        if s2:
            return s2
        if _looks_like_xml(s) or _is_mostly_printable_text(s):
            return s
        return msg_text

    data: Optional[bytes] = None
    if isinstance(compress_value, memoryview):
        data = bytes(compress_value)
    elif isinstance(compress_value, (bytes, bytearray)):
        data = bytes(compress_value)

    if not data:
        return msg_text

    if zstd is not None:
        try:
            out = _decompress_limited(data)
            s = out.decode("utf-8", errors="ignore")
            s = html.unescape(s.strip())
            if _looks_like_xml(s) or _is_mostly_printable_text(s):
                return s
        except MessageDecodeError:
            raise
        except Exception:
            pass

    try:
        s = data.decode("utf-8", errors="ignore")
        s = html.unescape(s.strip())
        s2 = try_decode_text_blob(s)
        if s2:
            return s2
        if _looks_like_xml(s) or _is_mostly_printable_text(s):
            return s
    except MessageDecodeError:
        raise
    except Exception:
        pass

    return msg_text
