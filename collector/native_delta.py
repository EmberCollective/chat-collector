"""Find conservative row candidates using SQLite's own dbstat/dbdata extensions."""
from __future__ import annotations

import ctypes as C
import json
from pathlib import Path
import re


def _register(db):
    if Path(db.lib._name).name != 'sqlcipher-qq-pages.dll':
        raise ValueError('QQ row candidates require the isolated QQ native engine')
    if getattr(db, '_dbdata_extension', None) is not None:
        return
    library = Path(__file__).resolve().parents[1] / 'runtime-deps/native/sqlite-dbdata-qq.dll'
    extension = C.CDLL(str(library))
    initialize = extension.sqlite3_dbdata_init
    initialize.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p]
    initialize.restype = C.c_int
    if initialize(db.db, None, None):
        raise RuntimeError('native_dbdata_registration_failed')
    db._dbdata_extension = extension


def capture_rows(db, changed_pages):
    """Capture before AND after a page update, then union the returned table/rowids.

    An interior-page change includes its whole subtree, so disappearing or reused
    leaves cannot lose deletions. Overflow changes include their owning row page.
    None means the table must use the existing full logical comparison.
    The caller must separately fall back on schema changes or a lost baseline.
    """
    dirty = set(changed_pages)
    if not dirty:
        return {}
    _register(db)
    tables = dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    touched = db.execute("""
        SELECT name,path,pageno,pagetype FROM dbstat
        WHERE name IN (SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%')
          AND pageno IN (SELECT value FROM json_each(?))
    """, (json.dumps(sorted(dirty)),))
    owners = {}
    for name, path, number, kind in touched:
        if kind == 'overflow':
            owners.setdefault(name, set()).add(path.rpartition('/')[0] + '/')
    leaves = {}
    for name, paths in owners.items():
        leaves.update(((name,path),number) for path,number in db.iterate("""
            SELECT path,pageno FROM dbstat WHERE name=? AND pagetype='leaf'
              AND path IN (SELECT value FROM json_each(?))
        """, (name,json.dumps(sorted(paths)))))
    pages = {}
    result = {}
    for name, path, number, kind in touched:
        if re.search(r'\bWITHOUT\s+ROWID\b', tables[name] or '', re.I):
            result[name] = None
            continue
        if kind == 'overflow':
            path = path.rpartition('/')[0] + '/'
            owner = leaves.get((name, path))
            if owner is None:
                result[name] = None
                continue
            number = owner
        pages.setdefault(name, {})[path] = number
    for name, roots in pages.items():
        if name in result:
            continue
        rows = set()
        included = []
        for path, root in sorted(roots.items(), key=lambda item: (len(item[0]), item[0])):
            if any(path.startswith(parent) for parent in included):
                continue
            included.append(path)
            rows.update(row[0] for row in db.iterate("""
                WITH RECURSIVE pages(pgno) AS (
                    VALUES(?) UNION
                    SELECT child FROM pages JOIN sqlite_dbptr AS pointer ON pointer.pgno=pages.pgno
                )
                SELECT DISTINCT data.value FROM pages JOIN sqlite_dbdata AS data ON data.pgno=pages.pgno
                WHERE data.field=-1
            """, (root,)))
        if any(type(row) is not int for row in rows):
            raise RuntimeError('native_rowid_candidates_invalid')
        result[name] = sorted(rows)
    return result


def merge_candidates(before, after):
    result = {}
    for name in before.keys() | after.keys():
        if before.get(name, []) is None or after.get(name, []) is None:
            result[name] = None
        else:
            result[name] = sorted(set(before.get(name, [])) | set(after.get(name, [])))
    return result
