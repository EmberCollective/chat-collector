import hashlib
import io
import os
import shutil
import tempfile
import argparse
import json
from pathlib import Path
import subprocess
import tarfile
import urllib.request
import zipfile
import re

parser=argparse.ArgumentParser(description='Build unmodified upstream SQLCipher with SQLite page access')
parser.add_argument('--toolchain',type=Path,default=Path('C:/Strawberry/c'))
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
work_context=tempfile.TemporaryDirectory(prefix='sqlcipher-pages-build-')
work=Path(work_context.name)
url='https://files.pythonhosted.org/packages/8b/89/6f67a4ea4e837f32b837808665c03b9caca82b3df699c870676b362d6df5/sqlcipher3-0.6.5.tar.gz'
expected='368a77f1bc8f6a5b88a7b0774bcf681dc8127511948727a359223629efd9d634'
data=urllib.request.urlopen(url,timeout=30).read()
if hashlib.sha256(data).hexdigest()!=expected:raise RuntimeError('Upstream archive checksum mismatch')
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
    for name in ['sqlite3.c','sqlite3.h']:
        (work/name).write_bytes(archive.extractfile('sqlcipher3-0.6.5/vendor/'+name).read())
out=root/'runtime-deps/native';out.mkdir(parents=True,exist_ok=True)
flags=['SQLITE_HAS_CODEC','SQLITE_TEMP_STORE=2','SQLITE_THREADSAFE=1',
       'SQLITE_EXTRA_INIT=sqlcipher_extra_init','SQLITE_EXTRA_SHUTDOWN=sqlcipher_extra_shutdown',
       'SQLITE_ENABLE_DBPAGE_VTAB','SQLITE_ENABLE_DBSTAT_VTAB','SQLCIPHER_CRYPTO_OPENSSL',
       'SQLITE_ENABLE_FTS5','SQLITE_ENABLE_FTS3','SQLITE_ENABLE_RTREE','SQLITE_USE_URI']
cmd=[str(args.toolchain/'bin/gcc.exe'),'-O2','-shared',str(work/'sqlite3.c'),'-o',str(out/'sqlcipher-pages.dll'),
     *['-D'+f for f in flags],'-I'+str(args.toolchain/'include'),'-L'+str(args.toolchain/'lib'),'-lcrypto','-lws2_32','-lcrypt32']
result=subprocess.run(cmd,capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
if result.returncode:raise RuntimeError(result.stderr[-6000:])
shutil.copyfile(out/'sqlcipher-pages.dll',out/'sqlcipher-qq-pages.dll')
shutil.copyfile(args.toolchain/'bin/libcrypto-3-x64__.dll',out/'libcrypto-3-x64__.dll')
shutil.copyfile(work/'sqlite3.c',out/'sqlite3.c')
record={'source_url':url,'archive_sha256':expected,'amalgamation_sha256':hashlib.sha256((work/'sqlite3.c').read_bytes()).hexdigest(),
        'defines':flags,'dll_sha256':hashlib.sha256((out/'sqlcipher-pages.dll').read_bytes()).hexdigest(),'source_modified':False}
(out/'build.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
dbdata_url='https://www.sqlite.org/2025/sqlite-src-3510000.zip'
dbdata_hash='5330719b8b80bf563991ff7a373052943f5357aae76cd1f3367eab845d3a75b7'
archive_bytes=urllib.request.urlopen(dbdata_url,timeout=60).read()
if hashlib.sha256(archive_bytes).hexdigest()!=dbdata_hash:raise RuntimeError('SQLite archive checksum mismatch')
with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
    dbdata=archive.read('sqlite-src-3510000/ext/recover/dbdata.c')
(work/'dbdata.c').write_bytes(dbdata)
# The copied DLL's embedded name still names the default engine: explicitly bind QQ's engine.
symbols=sorted(set(re.findall(r'(sqlite3_[a-z0-9_]+)\s*\(',dbdata.decode()))-{'sqlite3_dbdata_init'})
(work/'qq.def').write_text('LIBRARY sqlcipher-qq-pages.dll\nEXPORTS\n'+'\n'.join(symbols)+'\n')
commands=[
    [str(args.toolchain/'bin/dlltool.exe'),'-d',str(work/'qq.def'),'-l',str(work/'qq.a'),'-D','sqlcipher-qq-pages.dll'],
    [str(args.toolchain/'bin/gcc.exe'),'-shared','-O2',str(work/'dbdata.c'),str(work/'qq.a'),'-I'+str(work),'-o',str(out/'sqlite-dbdata-qq.dll')]]
for command in commands:
    built=subprocess.run(command,capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
    if built.returncode:raise RuntimeError(built.stderr[-6000:])
shutil.copyfile(work/'dbdata.c',out/'dbdata.c')
(out/'dbdata-build.json').write_text(json.dumps({'source_url':dbdata_url,'archive_sha256':dbdata_hash,
    'source_sha256':hashlib.sha256(dbdata).hexdigest(),'source_modified':False,'commands':commands,
    'dll_sha256':hashlib.sha256((out/'sqlite-dbdata-qq.dll').read_bytes()).hexdigest()},indent=2),encoding='utf-8')
work_context.cleanup()
print(json.dumps(record))
