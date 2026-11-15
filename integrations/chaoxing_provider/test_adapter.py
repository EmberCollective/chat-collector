import importlib.util,json,sqlite3,tempfile,unittest
from pathlib import Path
from integrations.chaoxing_provider.adapter import events_from_capture,publish
class ProviderTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent);self.root=Path(self.tmp.name);self.db=self.root/'capture.sqlite';c=sqlite3.connect(self.db)
  c.executescript('CREATE TABLE sources(id TEXT,enabled INTEGER,kind TEXT,since REAL);CREATE TABLE messages(id TEXT,source_id TEXT,body TEXT,sender TEXT,sent_ms REAL,captured REAL,baseline INTEGER);')
  c.executemany('INSERT INTO sources VALUES(?,?,?,?)',[('selected',1,'chat',100),('other',1,'chat',100),('revoked',0,'chat',100)])
  c.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',[('a','selected','synthetic text','unmapped',101000,110,0),('b','selected','old time','unmapped',99000,110,0),('c','selected','baseline','unmapped',101000,110,1),('d','other','private other','unmapped',101000,110,0),('e','selected','boundary','unmapped',101000,120,0),('f','selected','【非文字消息，尚未解析图片或附件】','unmapped',101000,110,0)])
  c.commit();c.close();self.before=self.db.read_bytes()
 def tearDown(self):self.tmp.cleanup()
 def events(self,start=100,end=120):return events_from_capture(self.db,'stable-account',['selected'],start,end)
 def test_scope_timestamp_baseline_and_source_read_only(self):
  events=self.events();self.assertEqual(len(events),1);self.assertEqual(events[0]['message']['sent_at'],101);self.assertEqual(events[0]['message']['sender_id'],'');self.assertEqual(events[0]['message']['identity']['people'],[]);self.assertNotIn(str(self.db),json.dumps(events));self.assertEqual(self.db.read_bytes(),self.before)
 def test_permission_and_windows(self):
  with self.assertRaises(ValueError):events_from_capture(self.db,'account',['revoked'],100,120)
  with self.assertRaises(ValueError):events_from_capture(self.db,'account',[],100,120)
  self.assertEqual(len(self.events(120,121)),1)
 def test_stable_ids_do_not_depend_on_scan_window(self):
  a=self.events()[0];b=self.events(109,115)[0];self.assertEqual(a['event_id'],b['event_id']);self.assertEqual(a['payload_sha256'],b['payload_sha256'])
 def test_upstream_contract_publication_retry_and_conflict(self):
  reference=Path('contracts.py')
  if not reference.exists():reference=Path('classroom-output/merge-reference/contracts.py')
  if not reference.exists():self.skipTest('Pass an independently reviewed upstream contracts.py to run compatibility check')
  spec=importlib.util.spec_from_file_location('upstream_contracts',reference);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  events=self.events();module.validate_event(events[0]);out=self.root/'batches';ledger=self.root/'ledger.sqlite'
  self.assertEqual(publish(events,module,out,ledger),1);self.assertEqual(publish(self.events(109,115),module,out,ledger),0);self.assertEqual(len(list(out.glob('*.jsonl'))),1)
  changed=self.events();changed[0]['payload_sha256']='changed'
  with self.assertRaises(ValueError):publish(changed,module,out,ledger)
 def test_failure_recovery_keeps_original_envelope_and_authorized_ids(self):
  class Contracts:
   fail=True;rows=[]
   def validate_event(self,e):pass
   def ingest_batch(self,rows,inbox):
    if self.fail:raise OSError('synthetic failure')
    self.rows.extend(rows)
  module=Contracts();ledger=self.root/'ledger.sqlite'
  with self.assertRaises(OSError):publish(self.events(),module,self.root/'batches',ledger)
  module.fail=False;self.assertEqual(publish([],module,self.root/'batches',ledger),0)
  self.assertEqual(publish(self.events(109,115),module,self.root/'batches',ledger),1);self.assertEqual(module.rows[0]['window']['start_inclusive'],100)
if __name__=='__main__':unittest.main()
