"""Independent CLI/scheduler entry; no n8n, original workspace, or downstream AI."""
import argparse,json,os,signal,threading,time
from contextlib import contextmanager
from pathlib import Path
from .reception import SchoolReception
from .transport import SourceError,load_session
from .sources import NativeSources
from .session import bootstrap_browser,private_directory,save_session

@contextmanager
def state_lock(root):
 stream=(Path(root)/'capture.lock').open('a+b')
 try:
  stream.seek(0,2)
  if not stream.tell():stream.write(b'0');stream.flush()
  stream.seek(0)
  try:
   if os.name=='nt':
    import msvcrt
    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
   else:
    import fcntl
    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
  except OSError:raise SourceError('collector_already_running',60) from None
  try:yield
  finally:
   stream.seek(0)
   if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
   else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
 finally:stream.close()

def collect_once(root):
 with state_lock(root):return _collect_once(root)

def _collect_once(root):
 service=SchoolReception(root)
 try:
  results=[]
  for source in service.status()['sources']:
   if not source['enabled'] or source['retry_at']>time.time():continue
   try:results.append({'source':source['id'],**service.poll_one(source['id'])})
   except SourceError as error:
    results.append({'source':source['id'],'error':error.code})
    if error.code.startswith('needs_') or error.code=='account_changed_use_new_state':break
  return {'read_only':True,'results':results}
 finally:service.close()

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state-dir',required=True,type=Path);sub=parser.add_subparsers(dest='command',required=True)
 sub.add_parser('init');sub.add_parser('status');sub.add_parser('catalog');sub.add_parser('once');run=sub.add_parser('run');run.add_argument('--interval',type=int,default=60)
 bootstrap=sub.add_parser('bootstrap');bootstrap.add_argument('--cdp-port',required=True,type=int)
 imp=sub.add_parser('import-session');imp.add_argument('--cookie-file',required=True,type=Path)
 permit=sub.add_parser('permit');permit.add_argument('--source',required=True);permit.add_argument('--keyword',default='');group=permit.add_mutually_exclusive_group(required=True);group.add_argument('--enable',action='store_true');group.add_argument('--disable',action='store_true')
 args=parser.parse_args();service=None
 try:
  root=args.state_dir.resolve()
  if args.command=='init':private_directory(root);print('{"initialized":true}');return
  if args.command=='bootstrap':print(json.dumps(bootstrap_browser(root,args.cdp_port)));return
  if args.command=='import-session':print(json.dumps(save_session(root,json.loads(args.cookie_file.read_text(encoding='utf-8-sig')))));return
  if not (root/'.initialized').is_file():raise SourceError('initialize_state_directory_first')
  if args.command=='once':print(json.dumps(collect_once(root)));return
  if args.command=='run':
   if args.interval<60:raise SourceError('interval_must_be_at_least_60_seconds')
   stop=threading.Event();signal.signal(signal.SIGINT,lambda *_:stop.set());signal.signal(signal.SIGTERM,lambda *_:stop.set())
   while not stop.is_set():
    result=collect_once(root);print(json.dumps(result),flush=True)
    if any(r.get('error','').startswith('needs_') or r.get('error')=='account_changed_use_new_state' for r in result['results']):break
    stop.wait(args.interval)
   return
  service=SchoolReception(root)
  if args.command=='catalog':service.catalog()
  elif args.command=='permit':service.configure({'id':args.source,'enabled':args.enable,'keyword':args.keyword})
  elif args.command=='status':
   try:NativeSources(load_session(root)).im_auth()
   except SourceError as error:print(json.dumps({'session_error':error.code,**service.status()}));return
  print(json.dumps(service.status(),ensure_ascii=True))
 except Exception as error:
  parser.exit(1,getattr(error,'code','portable_runtime_failed')+'\n')
 finally:
  if service:service.close()
if __name__=='__main__':main()
