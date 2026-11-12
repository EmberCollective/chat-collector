# WeChat offline core provenance

Extracted from the user's existing WeChatDataAnalysis checkout, version 2.10.1.
`upstream-hashes.json` identifies the original files by SHA-256.

- `decrypt.py`: required constants, helper functions and `WeChatDatabaseDecryptor` from `src/wechat_decrypt_tool/wechat_decrypt.py`; application-wide discovery, UI/API entry points and batch dispatch were omitted. The application logger import is replaced with Python's `logging.getLogger`.
- `sqlite_wal.py` and `sqlite_diagnostics.py`: copied unchanged, preserving WAL authentication, snapshot validation, index diagnostics and atomic publication behavior.
- `message_text.py`: original SQLite/zstd decoder and its three helper functions from `chat_helpers.py`, with only required imports. Every zstd expansion is now limited to 16 MiB, including nested/encoded paths; recognized compressed-data errors propagate instead of silently falling back to encoded text.

These are existing algorithms, not a new cryptographic implementation or reverse-engineering engine. No native DLL, lease, web server, AI model or frontend is required. Runtime dependencies are `cryptography` and `zstandard`.

The source checkout supplied no top-level LICENSE file. This extraction is for the user's local project; this provenance record does not assert a redistribution license.

Collection queries the actual decrypted `Msg_*` SQLite tables and their `Name2Id` mappings. The time predicate is `create_time >= window_start AND create_time < window_end`; custom SQL runs against a CTE exposing original columns plus normalized fields. No hidden ID watermark, timestamp overlap, AI classification or historical replay is added. Backdated late arrivals and older edits require an explicit time-range replay.

Refreshing decrypts only changed numbered `message_N.db` files and associated WAL content. It preserves original encrypted files and publishes a plain snapshot only after upstream validation. The refresh manifest is a file-change cache, not a message cursor. A source changing during its read fails the round and is retried by the caller; cross-database snapshots are sequential, not one global transaction.

Generated snapshots receive a SQLite index on `(create_time, local_id)` for each message table. The original databases have no timestamp index in this installation; this index keeps native time-window queries bounded. Existing externally supplied decrypted-only snapshots are read-only and are not altered by `scan`.
