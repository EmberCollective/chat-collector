"""Local school notice/chat reception. Only explicitly enabled sources are polled."""
import base64,hashlib,json,re,sqlite3,threading,time
from pathlib import Path
from .transport import load_session,SourceError
from .sources import NativeSources,IM
from .wire import fields
ROOT=Path(__file__).resolve().parent
NOTICE='https://notice.chaoxing.com/pc/notice/getNoticeList'
def source_is_notice_reenabled(previous,value):return previous['kind']=='notice' and value['enabled'] and not previous['enabled']
def identity(value):return hashlib.sha256(str(value).encode()).hexdigest()
def decode_chat(encoded):
 raw=base64.b64decode(encoded,validate=True);meta=dict(fields(raw));mid=str(meta.get(1,''));stamp=meta.get(4)
 if not mid or not isinstance(stamp,int):raise ValueError('invalid_message_identity')
 result={'native_id':mid,'sent_raw':str(stamp),'sent_ms':stamp if stamp>100_000_000_000 else stamp*1000,'body':'','sender':'发送者未映射'}
 if meta.get(5)!=1:return {**result,'control':True}
 body=fields(meta.get(6,b''));values=dict(body)
 if values.get(1,0) not in (0,1,2,3):return {**result,'control':True}
 jid=dict(fields(meta.get(2,b'')));sender=jid.get(2,b'').decode('utf-8','strict');result['sender']='发送者 '+identity(sender)[:10] if sender else result['sender'];texts=[]
 for key,data in body:
  if key!=4 or not isinstance(data,bytes):continue
  content=dict(fields(data))
  if content.get(1,0)==0 and isinstance(content.get(2),bytes):texts.append(content[2].decode('utf-8','strict'))
 result['body']='\n'.join(texts)[:100000] if texts else '【非文字消息，尚未解析图片或附件】'
 return result
class SchoolReception:
 def __init__(self,root=ROOT):
  self.root=Path(root);self.output=self.root/'classroom-output';self.output.mkdir(exist_ok=True);self.private=self.output/'school-reception';self.private.mkdir(exist_ok=True);self.path=self.private/'inbox.local.sqlite';self.lock=threading.RLock();self.poll_lock=threading.Lock();self.active=False;self.closed=False;self.wake=threading.Event();self.thread=None
  c=self.db()
  with c:c.execute("INSERT OR IGNORE INTO sources(id,kind,target,label) VALUES('notice','notice','','正式通知／收件箱')")
  c.close()
 def db(self):
  c=sqlite3.connect(self.path,timeout=5);c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL');c.executescript('''CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,kind TEXT,target TEXT,label TEXT,enabled INTEGER DEFAULT 0,version INTEGER DEFAULT 0,since REAL,anchor TEXT,last_check REAL,error TEXT,retry_at REAL DEFAULT 0,keyword TEXT DEFAULT ''); CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,source_id TEXT,title TEXT,body TEXT,sender TEXT,sent_raw TEXT,sent_ms REAL,captured REAL,baseline INTEGER,platform_read INTEGER);''');
  columns={r[1] for r in c.execute('PRAGMA table_info(sources)')}
  for name,kind in [('last_activity','REAL'),('unread_count','INTEGER')]:
   if name not in columns:c.execute('ALTER TABLE sources ADD COLUMN '+name+' '+kind)
  c.commit();return c
 def source(self,identifier):
  c=self.db()
  try:
   row=c.execute('SELECT * FROM sources WHERE id=?',(identifier,)).fetchone()
   if not row:raise ValueError('unknown_source')
   return dict(row)
  finally:c.close()
 def status(self):
  c=self.db()
  try:rows=[{k:r[k] for k in ('id','kind','label','enabled','last_check','error','retry_at','keyword','last_activity','unread_count')} for r in c.execute('SELECT * FROM sources ORDER BY kind,label')]
  finally:c.close()
  with self.lock:return {'receiving':self.active,'sources':rows,'read_only':True,'execution_enabled':False,'interval_seconds':60}
 def configure(self,value):
  if not isinstance(value,dict) or set(value)!={'id','enabled','keyword'} or type(value['enabled']) is not bool or not isinstance(value['keyword'],str) or len(value['keyword'])>200:raise ValueError('invalid_permission')
  with self.lock:
   previous=self.source(value['id']);c=self.db()
   try:
    with c:c.execute('UPDATE sources SET enabled=?,keyword=?,version=version+1,since=?,anchor=? WHERE id=?',(int(value['enabled']),value['keyword'],previous['since'] if previous['enabled'] else time.time(),None if source_is_notice_reenabled(previous,value) else previous['anchor'],value['id']))
   finally:c.close()
  self.wake.set();return self.status()
 def catalog(self):
  client=NativeSources(load_session(self.root));courses,_=client.catalog();heads,complete=client.heads()
  if not complete:raise SourceError('conversation_list_partial',300)
  names={c['chatid']:c['name'] for c in courses.values()};c=self.db()
  try:
   with c:
    for row in heads:
     kind=str(row.get('session_type','')).lower();target=str(row.get('session_to',''))
     if kind not in ('groupchat','chat','singlechat') or not re.fullmatch('[A-Za-z0-9_-]{1,128}',target):continue
     identifier='im-'+identity(kind+':'+target)[:20];label=names.get(target) or ('群聊' if kind=='groupchat' else '私聊')+' · '+identifier[-8:]
     # Last message ID is metadata only. No payload is retained during listing.
     anchor=str(row.get('meta',{}).get('id') or '')
     stamp=row.get('meta',{}).get('timestamp');stamp=stamp/1000 if isinstance(stamp,(int,float)) and stamp>100_000_000_000 else stamp if isinstance(stamp,(int,float)) else None
     c.execute('INSERT INTO sources(id,kind,target,label,anchor,last_activity,unread_count) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,last_activity=excluded.last_activity,unread_count=excluded.unread_count',(identifier,kind,target,label,anchor or None,stamp,row.get('unread_num')))
  finally:c.close()
  return self.status()
 def messages(self,source=None):
  if source is not None:self.source(source)
  c=self.db()
  try:
   query=' FROM messages m JOIN sources s ON s.id=m.source_id'+(' WHERE m.source_id=?' if source else '');params=(source,) if source else ()
   rows=c.execute('SELECT m.*,s.label AS source_label,s.kind AS source_kind'+query+' ORDER BY captured DESC,sent_ms DESC LIMIT 100',params).fetchall();return {'messages':[dict(r) for r in rows],'total':c.execute('SELECT COUNT(*)'+query,params).fetchone()[0],'read_only':True}
  finally:c.close()
 def poll_one(self,identifier):
  if not self.poll_lock.acquire(False):raise SourceError('reception_busy',60)
  try:
   source=self.source(identifier)
   if not source['enabled']:raise SourceError('permission_required',60)
   if source['retry_at']>time.time():raise SourceError('source_backoff',60)
   session=load_session(self.root);records=[];newest=None;reached=False;baseline=source['anchor'] is None;cursor=-1;seen=set()
   if source['kind']!='notice':client=NativeSources(session);uid,token=client.im_auth()
   for page in range(5):
    if source['kind']=='notice':
     params={'type':0,'notice_type':0,'sort':0}
     if cursor!=-1:params['lastValue']=cursor
     raw=session.json(NOTICE,params)
     if raw.get('status') is not True or not isinstance(raw.get('notices'),dict):raise SourceError('invalid_notice_response',300)
     data=raw['notices'];batch=data.get('list');last=bool(data.get('lastPage'));next_cursor=data.get('lastGetId')
     if not isinstance(batch,list):raise SourceError('invalid_notice_page',300)
     for row in batch:
      mid=str(row.get('uuid') or row.get('idCode') or '')
      if not mid:raise SourceError('missing_notice_identity',300)
      if newest is None:newest=mid
      if mid==source['anchor']:reached=True;break
      records.append({'native_id':mid,'title':str(row.get('title') or '')[:1000],'body':str(row.get('content') or '')[:100000],'sender':str(row.get('createrName') or '名称未返回')[:200],'sent_raw':str(row.get('sendTime') or row.get('insertTime') or '')[:100],'sent_ms':None,'read':1 if str(row.get('isread'))=='1' else 0 if str(row.get('isread'))=='0' else None})
     if baseline:reached=True
    else:
     suffix='@conference.easemob.com' if source['kind']=='groupchat' else '@easemob.com'
     raw=session.get(IM+'/users/'+uid+'/messageroaming',bearer=token,history={'queue':source['target']+suffix,'start':cursor,'end':-1,'pull_number':50,'is_positive':False,'msgType':''});data=json.loads(raw).get('data',{});batch=data.get('msgs');last=data.get('is_last') is True;next_cursor=data.get('next_key')
     if not isinstance(batch,list):raise SourceError('invalid_chat_history',300)
     for row in batch:
      try:message=decode_chat(row['msg'])
      except (ValueError,KeyError,TypeError,UnicodeError):raise SourceError('invalid_chat_message',300) from None
      if newest is None:newest=message['native_id']
      if message['native_id']==source['anchor']:reached=True;break
      if message['sent_ms']/1000 < source['since']:reached=True;break
      if not message.get('control'):records.append({**message,'title':'聊天消息','read':None})
    if last and source['anchor']=='empty':reached=True
    if reached or last:break
    if next_cursor in (None,'',-1) or str(next_cursor) in seen:raise SourceError('pagination_did_not_advance',300)
    seen.add(str(next_cursor));cursor=next_cursor
   if not reached and not baseline:raise SourceError('message_gap_unresolved',300)
   captured=time.time();c=self.db();inserted=0
   try:
    with self.lock,c:
     current=c.execute('SELECT enabled,version FROM sources WHERE id=?',(identifier,)).fetchone()
     if not current['enabled'] or current['version']!=source['version']:return {'discarded_after_permission_change':True}
     for row in reversed(records):
      if source['keyword'] and source['keyword'] not in row['title']+row['body']:continue
      key=identity(identifier+':'+row['native_id']);result=c.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',(key,identifier,row['title'],row['body'],row['sender'],row['sent_raw'],row['sent_ms'],captured,int(baseline and source['kind']=='notice'),row['read']));inserted+=result.rowcount
     c.execute('UPDATE sources SET anchor=?,last_check=?,error=NULL,retry_at=0 WHERE id=?',(newest or source['anchor'] or 'empty',captured,identifier))
   finally:c.close()
   return {'inserted':inserted,'examined':len(records),'baseline':baseline,'requests':session.requests}
  except SourceError as error:
   if error.code not in ('permission_required','source_backoff'):
    c=self.db()
    try:
     with c:c.execute('UPDATE sources SET error=?,retry_at=? WHERE id=?',(error.code,time.time()+max(60,error.retry_after),identifier))
    finally:c.close()
   raise
  except Exception:
   c=self.db()
   try:
    with c:c.execute('UPDATE sources SET error=?,retry_at=? WHERE id=?',('message_parse_or_storage_failed',time.time()+300,identifier))
   finally:c.close()
   raise SourceError('message_parse_or_storage_failed',300) from None
  finally:self.poll_lock.release()
 def start(self):
  with self.lock:
   if self.closed:raise ValueError('closed')
   if not any(s['enabled'] for s in self.status()['sources']):raise SourceError('no_enabled_sources',60)
   if self.active:return self.status()
   self.active=True
   if not self.thread:self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
   self.wake.set()
  return self.status()
 def stop(self):
  with self.lock:self.active=False;self.wake.set()
  return self.status()
 def loop(self):
  while not self.closed:
   self.wake.clear()
   if self.active:
    for source in self.status()['sources']:
     if not self.active or self.closed:break
     if not source['enabled'] or source['retry_at']>time.time() or source['last_check'] and time.time()-source['last_check']<60:continue
     try:self.poll_one(source['id'])
     except SourceError as e:
      if e.code.startswith('needs_'):self.stop();break
     except Exception:
      self.stop();break
   self.wake.wait(60)
 def close(self):self.closed=True;self.stop()
