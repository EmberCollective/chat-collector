"""Group-card reader extracted unchanged from the existing WeChatDataAnalysis checkout."""
from __future__ import annotations
import re
import sqlite3
from pathlib import Path
from typing import Optional


def _load_group_nickname_map_from_contact_db(
    contact_db_path: Path,
    chatroom_id: str,
    sender_usernames: list[str],
) -> dict[str, str]:
    """Best-effort mapping for group member nickname (aka group card) from contact.db.

    WeChat stores per-chatroom member nicknames in `contact.db.chat_room.ext_buffer` as a protobuf-like blob.
    This helper parses that blob and returns { sender_username -> group_nickname } for the requested senders.

    Notes:
    - Best-effort: never raises; returns {} on any failure.
    - Only resolves usernames included in `sender_usernames` to keep parsing cheap.
    """

    chatroom = str(chatroom_id or "").strip()
    if not chatroom.endswith("@chatroom"):
        return {}

    targets = list(dict.fromkeys([str(x or "").strip() for x in sender_usernames if str(x or "").strip()]))
    if not targets:
        return {}
    target_set = set(targets)

    def decode_varint(raw: bytes, offset: int) -> tuple[Optional[int], int]:
        value = 0
        shift = 0
        pos = int(offset)
        n = len(raw)
        while pos < n:
            byte = raw[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            if (byte & 0x80) == 0:
                return value, pos
            shift += 7
            if shift > 63:
                return None, n
        return None, n

    def iter_fields(raw: bytes):
        idx = 0
        n = len(raw)
        while idx < n:
            tag, idx_next = decode_varint(raw, idx)
            if tag is None or idx_next <= idx:
                break
            idx = idx_next
            field_no = int(tag) >> 3
            wire_type = int(tag) & 0x7

            if wire_type == 0:
                _, idx_next = decode_varint(raw, idx)
                if idx_next <= idx:
                    break
                idx = idx_next
                continue

            if wire_type == 2:
                size, idx_next = decode_varint(raw, idx)
                if size is None or idx_next <= idx:
                    break
                idx = idx_next
                end = idx + int(size)
                if end > n:
                    break
                chunk = raw[idx:end]
                idx = end
                yield field_no, wire_type, chunk
                continue

            if wire_type == 1:
                idx += 8
                continue
            if wire_type == 5:
                idx += 4
                continue
            break

    def is_strong_username_hint(s: str) -> bool:
        v = str(s or "").strip()
        return v.startswith("wxid_") or v.endswith("@chatroom") or v.startswith("gh_") or ("@" in v)

    def looks_like_username(s: str) -> bool:
        v = str(s or "").strip()
        if not v:
            return False
        if is_strong_username_hint(v):
            return True
        # Common alias-style WeChat IDs are ASCII-ish and do not contain whitespace.
        if len(v) < 6 or len(v) > 32:
            return False
        if re.search(r"\s", v):
            return False
        if not re.match(r"^[A-Za-z][A-Za-z0-9_-]+$", v):
            return False
        if v.isdigit():
            return False
        return True

    def pick_display(
        strings: list[tuple[int, str]],
        target: str,
        *,
        allowed_fields: set[int],
    ) -> str:
        best_score = -1
        best = ""
        for i, (fno, value) in enumerate(strings):
            if int(fno) not in allowed_fields:
                continue
            v = str(value or "").strip()
            if (not v) or v == target:
                continue
            if is_strong_username_hint(v):
                continue
            if "\n" in v or "\r" in v:
                continue
            if len(v) > 64:
                continue

            score = 0
            if int(fno) == 2:
                score += 100
            if not looks_like_username(v):
                score += 20
            score += max(0, 32 - len(v))
            # Stable tie-breaker: prefer earlier appearance.
            score = score * 1000 - i
            if score > best_score:
                best_score = score
                best = v
        return best

    try:
        conn = sqlite3.connect(Path(contact_db_path).resolve().as_uri() + '?mode=ro', uri=True)
    except Exception:
        return {}

    try:
        row = conn.execute(
            "SELECT ext_buffer FROM chat_room WHERE username = ? LIMIT 1",
            (chatroom,),
        ).fetchone()
        if row is None:
            return {}

        ext = row[0]
        if ext is None:
            return {}
        if isinstance(ext, memoryview):
            ext_buf = ext.tobytes()
        elif isinstance(ext, (bytes, bytearray)):
            ext_buf = bytes(ext)
        else:
            return {}
        if not ext_buf:
            return {}

        member_records: list[list[tuple[int, str]]] = []
        for _, wire_type, chunk in iter_fields(ext_buf):
            if wire_type != 2 or (not chunk):
                continue

            # Parse submessage and collect UTF-8 strings.
            strings: list[tuple[int, str]] = []
            try:
                for sfno, swire, sval in iter_fields(chunk):
                    if swire != 2:
                        continue
                    if not sval:
                        continue
                    if len(sval) > 256:
                        continue
                    try:
                        txt = bytes(sval).decode("utf-8", errors="strict")
                    except Exception:
                        continue
                    txt = txt.strip()
                    if not txt:
                        continue
                    strings.append((int(sfno), txt))
            except Exception:
                continue

            if not strings:
                continue
            member_records.append(strings)

        # Layout A stores the member username in field 1 and its optional group
        # nickname in field 2. A chatroom with no group nicknames can therefore
        # omit field 2 entirely, while field 4 may still reference another
        # member. Treat a requested username in field 1 as positive evidence of
        # the current layout; do not infer the legacy layout merely because the
        # whole chatroom lacks field 2.
        uses_current_layout = any(
            field_no == 2 or (field_no == 1 and value in target_set)
            for strings in member_records
            for field_no, value in strings
        )

        out: dict[str, str] = {}
        if uses_current_layout:
            for strings in member_records:
                primary_targets = [
                    value
                    for field_no, value in strings
                    if field_no == 1 and value in target_set and value not in out
                ]
                for target in primary_targets:
                    disp = pick_display(strings, target, allowed_fields={2})
                    if disp:
                        out[target] = disp
                if len(out) >= len(target_set):
                    break
            return out

        # Legacy field 4 -> field 1 mapping is allowed only with positive
        # evidence that every relevant field-1 value is display text rather
        # than an alias-style username. Ambiguous ASCII values fail closed and
        # let the existing caller fallback resolve the display name.
        legacy_records = [
            strings
            for strings in member_records
            if any(field_no == 4 and value in target_set for field_no, value in strings)
        ]
        if not legacy_records:
            return {}
        for strings in legacy_records:
            field1_values = [
                str(value or "").strip()
                for field_no, value in strings
                if field_no == 1 and str(value or "").strip()
            ]
            if not field1_values or any(looks_like_username(value) for value in field1_values):
                return {}

        for strings in legacy_records:
            legacy_targets = [
                value
                for field_no, value in strings
                if field_no == 4 and value in target_set and value not in out
            ]
            for target in legacy_targets:
                disp = pick_display(strings, target, allowed_fields={1})
                if disp:
                    out[target] = disp
            if len(out) >= len(target_set):
                break

        return out
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass
