"""User-session runner for the unchanged qq-nt-export read-only key scanner.

No privilege escalation, injection, network access, or QQ restart. Candidate
credentials are accepted only after official SQLCipher authenticates a snapshot.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runtime-deps"))
sys.path.insert(0, str(ROOT))
from collector.config import load_config
CONFIG = load_config(ROOT / "config/qq.toml")
SOURCE = Path(CONFIG["source"]["encrypted_dir"]) / "nt_msg.db"
ACCOUNT = CONFIG["source"]["account_id"]
KEY_FILE = Path(CONFIG["source"]["key_file"])
PRIVATE = KEY_FILE.parent / "qq-recovery"


def source_stamp():
    result = []
    for path in (SOURCE, Path(str(SOURCE) + "-wal")):
        try:
            stat = path.stat()
            result.append((stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            result.append(None)
    return result


def main():
    import sqlcipher3.dbapi2 as sqlcipher

    PRIVATE.mkdir(parents=True, exist_ok=True)
    scanner_root = Path(__file__).resolve().parent / "qq-nt-export"
    (scanner_root / "config.json").write_text("{}", encoding="utf-8")
    report = PRIVATE / "scan.private.json"
    report.unlink(missing_ok=True)
    print("正在只读检查已登录的 QQ，请保持 QQ 打开。")
    private_log = PRIVATE / "scan.private.log"
    try:
        with private_log.open("wb") as log:
            subprocess.run([sys.executable, str(scanner_root / "scripts" / "ntqq_key_scan.py"),
                            "--src-db", str(SOURCE), "--json-out", str(report), "--no-write-config"],
                           stdout=log, stderr=subprocess.STDOUT, timeout=240, check=False)
        if not report.exists():
            raise RuntimeError("scanner_no_report")
        result = json.loads(report.read_text(encoding="utf-8"))
    finally:
        private_log.unlink(missing_ok=True)
        report.unlink(missing_ok=True)
    (PRIVATE / "scan-status.json").write_text(json.dumps({
        "scan_stats": result.get("scan_stats", []),
        "matching_candidates": len(result.get("confirmed", []))}), encoding="utf-8")
    candidates = {item["key_hex"].lower() + item["salt_hex"].lower()
                  for item in result.get("confirmed", []) if item.get("salt_match")}
    if not candidates:
        denied = any(item.get("error") for item in result.get("scan_stats", []))
        print("未能读取 QQ 进程。请右键 qq-recover.cmd，选择以管理员身份运行。" if denied else
              "未找到匹配凭据。请确认账号已登录，并将结果告知助手。")
        return 1
    print("找到候选凭据；正在通过 SQLCipher 验证数据库副本。")
    with tempfile.TemporaryDirectory(prefix="verify-", dir=PRIVATE) as temp:
        snapshot = Path(temp) / "encrypted.db"
        snapshot_wal = Path(str(snapshot) + "-wal")
        for attempt in range(3):
            before = source_stamp()
            with SOURCE.open("rb") as src, snapshot.open("wb") as dst:
                src.seek(1024)
                shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
            snapshot_wal.unlink(missing_ok=True)
            try:
                wal = Path(str(SOURCE) + "-wal")
                if wal.exists():
                    shutil.copyfile(wal, snapshot_wal)
            except FileNotFoundError:
                continue
            if before == source_stamp():
                break
        else:
            print("QQ 数据库持续变化，未完成稳定副本验证。请稍后再运行一次。")
            return 1
        for key in sorted(candidates):
            if len(key) != 96 or any(c not in "0123456789abcdef" for c in key):
                continue
            conn = sqlcipher.connect(str(snapshot), isolation_level=None)
            try:
                conn.execute("PRAGMA cipher_page_size=4096")
                conn.execute('PRAGMA key="x\'' + key + '\'"')
                conn.execute("PRAGMA kdf_iter=4000")
                conn.execute("PRAGMA cipher_hmac_algorithm=HMAC_SHA1")
                conn.execute("PRAGMA cipher_kdf_algorithm=PBKDF2_HMAC_SHA512")
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"group_msg_table", "c2c_msg_table"} & tables:
                    continue
                # Authenticate at least one message-table page, without displaying chat content.
                table = "group_msg_table" if "group_msg_table" in tables else "c2c_msg_table"
                conn.execute('SELECT 1 FROM "' + table + '" LIMIT 1').fetchone()
            except sqlcipher.DatabaseError:
                continue
            finally:
                conn.close()
            pending = KEY_FILE.with_suffix(".tmp")
            pending.write_text(json.dumps({"account": ACCOUNT, "key_mode": "raw_key_salt", "key": key}), encoding="utf-8")
            os.replace(pending, KEY_FILE)
            (PRIVATE / "status.json").write_text(json.dumps({"account": ACCOUNT, "verified": True,
                "key_file": str(KEY_FILE), "validation": "SQLCipher schema and message-table authentication"}), encoding="utf-8")
            print("验证成功，凭据已保存在本机 secrets/account-key.json。请告诉助手继续解密。")
            return 0
    print("候选凭据未通过 SQLCipher 验证；未替换已存凭据。")
    return 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:
        # Exception details might contain sensitive paths/data: keep them private.
        PRIVATE.mkdir(parents=True, exist_ok=True)
        (PRIVATE / "error.private.txt").write_text(type(exc).__name__ + ": " + str(exc), encoding="utf-8")
        print("未完成验证，错误类型：" + type(exc).__name__ + "。请将此结果告知助手。")
        code = 1
    sys.exit(code)
