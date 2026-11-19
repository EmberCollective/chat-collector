"""Read-only Chaoxing captured-message provider for chat-collector schema v1.
No network, credentials, models, or platform actions. Publishing is separate.
"""
import hashlib,json,sqlite3
from datetime import datetime,timezone
from pathlib import Path

def digest(value):
 return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def events_from_capture(database,account_id,source_ids,start,end):
 if not isinstance(account_id,str) or not account_id.strip():raise ValueError('account_id_required')
 if not source_ids or len(set(source_ids))!=len(source_ids):raise ValueError('explicit_unique_sources_required')
 if type(start) is not int or type(end) is not int or end<=start:raise ValueError('invalid_half_open_window')
 database=Path(database).resolve()
 if not database.is_file():raise ValueError('capture_database_missing')
 conn=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
 try:
  output=[]
  for source_id in source_ids:
   source=conn.execute('SELECT * FROM sources WHERE id=?',(source_id,)).fetchone()
   if not source or not source['enabled']:raise ValueError('source_missing_or_not_authorized')
   # v1 currently exports verified ordinary text only. Never pretend HTML/attachments are parsed.
   if source['kind'] not in ('chat','singlechat','groupchat'):raise ValueError('source_type_not_supported_yet')
   for row in conn.execute('SELECT * FROM messages WHERE source_id=? AND baseline=0 AND captured>=? AND captured<? ORDER BY captured,id',(source_id,start,end)):
    if row['sent_ms'] is None or row['sent_ms']<source['since']*1000:continue
    if row['body'].startswith('【非文字'):continue
    key=digest(['chaoxing',account_id,source_id,row['id']])
    message={'platform':'chaoxing','account_id':account_id,'conversation_id':source_id,'conversation_type':'group' if source['kind']=='groupchat' else 'direct','message_id':row['id'],'sender_id':'','sender':'名称未获取','sent_at':int(row['sent_ms']//1000),'text':row['body'],'kind':'text','attachments':[],
     'identity':{'account':{'id':account_id},'conversation':{'id':source_id,'display_name':None,'missing_fields':['verified_name']},'people':[],'warnings':['sender_identity_unresolved','conversation_name_unverified']},
     'source':{'provider':'chaoxing-capture-bridge','record_id':row['id']},'extensions':{'chaoxing':{'identity_basis':'stable_local_capture_identity','sender_mapping':'unavailable'}}}
    payload=digest(message)
    output.append({'schema_version':1,'event_id':digest([key,1]),'message_key':key,'revision':1,'operation':'upsert','payload_sha256':payload,'collected_at':datetime.fromtimestamp(row['captured'],timezone.utc).isoformat(),'window':{'start_inclusive':start,'end_exclusive':end},'matched_rules':[],'message':message})
  return output
 finally:conn.close()

def publish(events,contracts,inbox,ledger):
 """One immutable file per event; retry uses the exact saved envelope.
 Changed content is rejected: immutable capture v1 does not implement revisions.
 The original batch window/time survives retries and overlapping scan windows.
 """
 ledger=Path(ledger);ledger.parent.mkdir(parents=True,exist_ok=True)
 conn=sqlite3.connect(ledger);conn.execute('CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,envelope TEXT NOT NULL,published INTEGER NOT NULL DEFAULT 0)')
 try:
  for event in events:
   contracts.validate_event(event)
   with conn:
    prior=conn.execute('SELECT payload_hash FROM outbox WHERE event_id=?',(event['event_id'],)).fetchone()
    if prior and prior[0]!=event['payload_sha256']:raise ValueError('capture_mutation_requires_revision_support')
    conn.execute('INSERT OR IGNORE INTO outbox(event_id,payload_hash,envelope) VALUES(?,?,?)',(event['event_id'],event['payload_sha256'],json.dumps(event,ensure_ascii=False)))
  count=0;allowed={event['event_id'] for event in events}
  for event_id,envelope in conn.execute('SELECT event_id,envelope FROM outbox WHERE published=0 ORDER BY event_id').fetchall():
   if event_id not in allowed:continue
   contracts.ingest_batch([json.loads(envelope)],inbox)
   with conn:conn.execute('UPDATE outbox SET published=1 WHERE event_id=?',(event_id,))
   count+=1
  return count
 finally:conn.close()
