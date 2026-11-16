"""Find row candidates from changed leaves and changes in table page ownership."""
from __future__ import annotations

from array import array
import ctypes as C
from dataclasses import dataclass
from itertools import zip_longest
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


@dataclass
class RowCapture:
    rows: dict
    tables: tuple
    leaf_tables: array
    dirty: set


def _rowids(db, pages):
    rows = {row[0] for row in db.iterate('''
        SELECT data.value FROM json_each(?) AS page
        JOIN sqlite_dbdata AS data ON data.pgno=page.value WHERE data.field=-1
    ''', (json.dumps(sorted(pages)),))}
    if any(type(row) is not int for row in rows):
        raise RuntimeError('native_rowid_candidates_invalid')
    return rows


def capture_rows(db, changed_pages):
    """Read changed row pages; retain compact leaf ownership for the before/after diff.

    The caller must merge both captures on the verified after image, with unchanged
    schema and a complete dirty-page set. An untouched detached leaf still contains
    its old rowids; overwritten leaves have already been read in the before capture.
    """
    dirty = set(changed_pages)
    if not dirty:
        return RowCapture({}, (), array('I'), dirty)
    _register(db)
    tables = db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    names = ('',) + tuple(name for name, _ in tables)
    identities = {name: index for index, name in enumerate(names)}
    unkeyed = {name for name, sql in tables if re.search(r'\bWITHOUT\s+ROWID\b', sql or '', re.I)}
    leaves = array('I', [0]) * (db.execute('PRAGMA page_count')[0][0] + 1)
    pages, result = {}, {}
    for name, _ in tables:
        owner = None
        # One table per query lets DBSTAT supply this order without a temp sort.
        for path, number, kind in db.iterate('''
            SELECT path,pageno,pagetype FROM dbstat WHERE name=? ORDER BY name,path
        ''', (name,)):
            if kind == 'leaf':
                leaves[number] = identities[name]
                owner = (path, number)
            if number not in dirty:
                continue
            if name in unkeyed:
                result[name] = None
            elif kind == 'leaf':
                pages.setdefault(name, set()).add(number)
            elif kind == 'overflow':
                if owner is None or owner[0] != path.rpartition('/')[0] + '/':
                    result[name] = None
                else:
                    pages.setdefault(name, set()).add(owner[1])
    for name, numbers in pages.items():
        if name not in result:
            result[name] = sorted(_rowids(db, numbers))
    return RowCapture(result, names, leaves, dirty)


def merge_candidates(before, after, db):
    if before.tables != after.tables or before.dirty != after.dirty:
        raise ValueError('Row captures must describe the same schema and dirty pages')
    result = {}
    for name in before.rows.keys() | after.rows.keys():
        if before.rows.get(name, []) is None or after.rows.get(name, []) is None:
            result[name] = None
        else:
            result[name] = sorted(set(before.rows.get(name, [])) | set(after.rows.get(name, [])))
    moved = {}
    for page, (old, new) in enumerate(zip_longest(before.leaf_tables, after.leaf_tables, fillvalue=0)):
        if old != new and page not in before.dirty:
            for identity in {old, new} - {0}:
                moved.setdefault(before.tables[identity], set()).add(page)
    for name, pages in moved.items():
        if result.get(name, []) is not None:
            result[name] = sorted(set(result.get(name, [])) | _rowids(db, pages))
    return result
