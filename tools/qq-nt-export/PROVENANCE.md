# Unmodified upstream scanner

- Repository: https://github.com/q957506633/qq-nt-export
- Source: https://raw.githubusercontent.com/q957506633/qq-nt-export/main/scripts/ntqq_key_scan.py
- Retrieved: 2026-11-12
- License: MIT, reproduced in LICENSE
- File SHA256: 484439bd0debe8b7797090a6d406a4c31b8fde0ff6f32fb3c06e8b40525ba91d

`scripts/ntqq_key_scan.py` is copied without modification. The local runner only
orchestrates it and verifies candidates with official SQLCipher. It does not
replace its memory scanner. No network, process writes, injection, or restarts.

Run `tools/qq-recover.cmd` manually with QQ logged in. On this machine, right-click
the launcher and choose Run as administrator; the runner does not elevate itself. It suppresses upstream
secret output, removes its temporary raw reports, and stores only a SQLCipher
verified credential in `secrets/account-key.json`. Source databases are read
only. A temporary full DB+WAL copy requires roughly 1.5 GB free space.
