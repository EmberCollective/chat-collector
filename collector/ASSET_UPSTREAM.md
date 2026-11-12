# Attachment archive provenance and behavior

`asset_vendor.py` copies only the existing WeChatDataAnalysis media routines needed for DAT V1/V2/XOR decoding, packed-info MD5 extraction and the shipped VoipEngine WXGF decoder. Original file SHA-256 values are in `asset-upstream-hashes.json`. Changes are explicit decoder DLL injection, absence of FFmpeg fallback, and an output-length bounds check. No cryptographic algorithm or native image decoder was invented.

`AttachmentArchive(config, platform, account_id).enrich(message)` receives the whole module configuration. It runs **after database SQL filtering**. It does not decide which messages to collect.

- `[attachments]`: `enabled`, `archive_dir`, optional `allowed_roots`.
- `[source]`: `media_dir`, `decrypted_dir`, `key_file`; optional `resource_db`, `image_cache_dir`, `decoder_dll`.
- The explicitly configured `media_dir` and `image_cache_dir` are trusted roots; `allowed_roots` permits additional explicit download locations.
- Files outside those roots, ambiguous names, missing files, unsupported media, and encrypted files without a usable decoder receive explicit statuses. No network download is attempted.
- Archives are named by SHA-256. Each copy is flushed, rehashed, and atomically published. Original media is never moved or deleted. A file changing during copy is rejected for retry.
- Images retain matching encrypted DAT sources alongside decoded copies. A cache-only image is marked `cache_quality_unknown`; a thumbnail-only source is marked `thumbnail_only`. The archive never claims an original-resolution image solely from its size or filename.
- Filename indexing happens lazily once per root per round. Message-to-image MD5 lookup uses current message packed-info, followed by the account's message-resource database, before falling back to attachment metadata.

Additional runtime dependencies: Pillow and pycryptodome. The native decoder is optional and supplied separately; encrypted media remains archived if conversion cannot run.

The inherited `_WxAMConfig` was 8 bytes, but Craft native disassembly of `wxam_dec_wxam2pic_5` (VoipEngine SHA256 `5fef396573ea80e3c92bdc4be1e4ac04f099aa805b74d7f046ff01d16323537e`) shows two 16-byte reads from its fifth argument. The extracted wrapper now supplies 32 zero-initialized bytes. Evidence `ev_a4bdabedba76fb5ed873518ea7711f3df5668acd9b613569a95f1058430b20c8`; Ghidra callee evidence `ev_5f68075885cc81b8af70f346ad37eacac2ce83f151a34a7b210d53d3cf7b0125`. This corrects the native calling interface, not the decoder implementation.

QQ image variants are retained separately. Original-content MD5 matches take priority; thumbnails and changed cached representations are preserved with explicit `thumbnail_only` or `unverified` status and actual hashes. Filename association alone never claims original-content verification. QQ `::NTOSFull::` is removed from database paths before allowlist validation.
