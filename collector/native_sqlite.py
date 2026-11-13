"""Small C-API binding to the unmodified SQLCipher build with SQLite dbpage enabled."""
from __future__ import annotations

import ctypes as C
import os
from pathlib import Path


class NativeSQLite:
    def __init__(self, path, *, readonly=False, library=None, pending_byte=None):
        library = Path(library or Path(__file__).resolve().parents[1]/'runtime-deps/native/sqlcipher-pages.dll').resolve()
        self._dll_dirs = []
        if os.name == 'nt':
            for directory in (library.parent, Path('C:/Strawberry/c/bin')):
                if directory.is_dir():
                    self._dll_dirs.append(os.add_dll_directory(str(directory)))
        self.lib = C.CDLL(str(library.resolve()))
        if pending_byte is not None:
            if library.name != 'sqlcipher-qq-pages.dll':
                raise ValueError('QQ pending byte requires its isolated native library')
            self.lib.sqlite3_test_control(11, C.c_uint(pending_byte))
        signatures = {
            'sqlite3_open_v2': ([C.c_char_p,C.POINTER(C.c_void_p),C.c_int,C.c_char_p],C.c_int),
            'sqlite3_close_v2': ([C.c_void_p],C.c_int),
            'sqlite3_errmsg': ([C.c_void_p],C.c_char_p),
            'sqlite3_prepare_v2': ([C.c_void_p,C.c_char_p,C.c_int,C.POINTER(C.c_void_p),C.c_void_p],C.c_int),
            'sqlite3_step': ([C.c_void_p],C.c_int),
            'sqlite3_finalize': ([C.c_void_p],C.c_int),
            'sqlite3_bind_int64': ([C.c_void_p,C.c_int,C.c_int64],C.c_int),
            'sqlite3_bind_null': ([C.c_void_p,C.c_int],C.c_int),
            'sqlite3_bind_text': ([C.c_void_p,C.c_int,C.c_char_p,C.c_int,C.c_void_p],C.c_int),
            'sqlite3_bind_blob': ([C.c_void_p,C.c_int,C.c_void_p,C.c_int,C.c_void_p],C.c_int),
            'sqlite3_column_count': ([C.c_void_p],C.c_int),
            'sqlite3_column_type': ([C.c_void_p,C.c_int],C.c_int),
            'sqlite3_column_int64': ([C.c_void_p,C.c_int],C.c_int64),
            'sqlite3_column_double': ([C.c_void_p,C.c_int],C.c_double),
            'sqlite3_column_blob': ([C.c_void_p,C.c_int],C.c_void_p),
            'sqlite3_column_bytes': ([C.c_void_p,C.c_int],C.c_int),
            'sqlite3_deserialize': ([C.c_void_p,C.c_char_p,C.c_void_p,C.c_int64,C.c_int64,C.c_uint],C.c_int),
        }
        for name,(args,result) in signatures.items():
            function=getattr(self.lib,name);function.argtypes=args;function.restype=result
        self.db=C.c_void_p()
        filename=':memory:' if str(path)==':memory:' else str(Path(path).resolve())
        code=self.lib.sqlite3_open_v2(filename.encode('utf-8'),C.byref(self.db),1 if readonly else 6,None)
        if code:
            self.close()
            raise RuntimeError('native_sqlite_open_failed')
        self.execute('PRAGMA busy_timeout=5000')
        self.execute('PRAGMA cipher_log_level=NONE')

    def execute(self, sql, parameters=()):
        return list(self.iterate(sql, parameters))

    def iterate(self, sql, parameters=()):
        statement=C.c_void_p()
        def check(code):
            if code != 0:
                error=self.lib.sqlite3_errmsg(self.db).decode('utf-8',errors='replace')
                raise RuntimeError('native_sqlite: '+error)
        check(self.lib.sqlite3_prepare_v2(self.db,sql.encode('utf-8'),-1,C.byref(statement),None))
        try:
            for index,value in enumerate(parameters,1):
                if value is None:code=self.lib.sqlite3_bind_null(statement,index)
                elif isinstance(value,int):code=self.lib.sqlite3_bind_int64(statement,index,value)
                elif isinstance(value,bytes):code=self.lib.sqlite3_bind_blob(statement,index,value,len(value),C.c_void_p(-1))
                elif isinstance(value,str):
                    raw=value.encode('utf-8');code=self.lib.sqlite3_bind_text(statement,index,raw,len(raw),C.c_void_p(-1))
                else:raise TypeError('Unsupported native SQLite parameter')
                check(code)
            while True:
                code=self.lib.sqlite3_step(statement)
                if code==101:break
                if code!=100:check(code)
                row=[]
                for column in range(self.lib.sqlite3_column_count(statement)):
                    kind=self.lib.sqlite3_column_type(statement,column)
                    if kind==5:value=None
                    elif kind==1:value=self.lib.sqlite3_column_int64(statement,column)
                    elif kind==2:value=self.lib.sqlite3_column_double(statement,column)
                    else:
                        size=self.lib.sqlite3_column_bytes(statement,column)
                        pointer=self.lib.sqlite3_column_blob(statement,column)
                        value=C.string_at(pointer,size) if pointer else b''
                        if kind==3:value=value.decode('utf-8',errors='strict')
                    row.append(value)
                yield tuple(row)
        finally:
            self.lib.sqlite3_finalize(statement)

    def deserialize(self, data):
        self._image=data
        self._image_buffer=(C.c_ubyte*len(data)).from_buffer(data)
        if self.lib.sqlite3_deserialize(self.db,b'main',self._image_buffer,len(data),len(data),4):
            raise RuntimeError('native_snapshot_deserialize_failed')

    def close(self):
        if getattr(self,'db',None):
            self.lib.sqlite3_close_v2(self.db);self.db=None
        for directory in self._dll_dirs:directory.close()
        self._dll_dirs=[]

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
