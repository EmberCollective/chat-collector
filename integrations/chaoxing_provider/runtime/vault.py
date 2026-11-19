"""Windows CurrentUser DPAPI credentials; plaintext stays in process memory."""
import ctypes
import json
import os
from pathlib import Path

MAGIC=b'CLASSROOM_LOGIN_V1\x00'


class _Blob(ctypes.Structure):
    _fields_=[('size',ctypes.c_ulong),('data',ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(raw,protect):
    if os.name!='nt':raise OSError('This credential vault requires Windows')
    def blob(value):
        buffer=ctypes.create_string_buffer(value)
        return _Blob(len(value),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte))),buffer
    source,source_buf=blob(raw);entropy,entropy_buf=blob(b'classroom-chaoxing-phone-v1');output=_Blob()
    api=ctypes.WinDLL('crypt32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
    if protect:
        fn=api.CryptProtectData;fn.argtypes=[ctypes.POINTER(_Blob),ctypes.c_wchar_p,ctypes.POINTER(_Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(_Blob)]
        args=[ctypes.byref(source),'Classroom login',ctypes.byref(entropy),None,None,1,ctypes.byref(output)]
    else:
        fn=api.CryptUnprotectData;fn.argtypes=[ctypes.POINTER(_Blob),ctypes.c_void_p,ctypes.POINTER(_Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(_Blob)]
        args=[ctypes.byref(source),None,ctypes.byref(entropy),None,None,1,ctypes.byref(output)]
    fn.restype=ctypes.c_int
    try:
        if not fn(*args):raise OSError('Windows could not protect or unlock the login credential')
        return ctypes.string_at(output.data,output.size)
    finally:
        ctypes.memset(source_buf,0,len(source_buf));ctypes.memset(entropy_buf,0,len(entropy_buf))
        if output.data:
            ctypes.memset(output.data,0,output.size);kernel.LocalFree(ctypes.cast(output.data,ctypes.c_void_p))

