import base64,json,os,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from integrations.chaoxing_provider.runtime.reception import SchoolReception,decode_chat
from integrations.chaoxing_provider.runtime.sources import NativeSources
from integrations.chaoxing_provider.runtime.transport import SourceError,ReadSession,approved_url
from integrations.chaoxing_provider.runtime.__main__ import collect_once,state_lock
class RuntimeTests(unittest.TestCase):
 def setUp(self):
  network=patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('live_network_forbidden_in_tests'));network.start();self.addCleanup(network.stop)
  self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent);self.root=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def test_clean_folder_runs_without_original_project(self):
  target=self.root/'delivery';shutil.copytree(Path(__file__).parent,target,ignore=shutil.ignore_patterns('__pycache__','tmp*','*.sqlite*'))
  result=subprocess.run([sys.executable,'-m','runtime','--help'],cwd=target,capture_output=True,text=True,env={**os.environ,'PYTHONPATH':''},timeout=10)
  self.assertEqual(result.returncode,0,result.stderr);self.assertIn('--state-dir',result.stdout)
 def test_default_denied_and_no_implicit_network(self):
  with patch('integrations.chaoxing_provider.runtime.reception.load_session',side_effect=AssertionError('no network')):
   r=SchoolReception(self.root)
   with self.assertRaises(SourceError):r.start()
   r.close();self.assertEqual(collect_once(self.root)['results'],[])
 def test_account_change_rejected_before_history(self):
  class Fake:
   account_hash='different-account'
   def json(self,*args,**kwargs):return {'result':1,'msg':{'uid':'synthetic-account','hxToken':'synthetic-token'}}
  with self.assertRaises(SourceError) as error:NativeSources(Fake()).im_auth()
  self.assertEqual(error.exception.code,'account_changed_use_new_state')
 def test_text_capture_transaction_and_repeat(self):
  r=SchoolReception(self.root);c=r.db()
  with c:c.execute("INSERT INTO sources(id,kind,target,label,anchor) VALUES('private','chat','synthetic-peer','unmapped','empty')")
  c.close();r.configure({'id':'private','enabled':True,'keyword':''})
  # Replay a deterministic bounded text payload after permission, without network.
  from .runtime.wire import fields
  def var(n):
   out=bytearray()
   while n>127:out.append((n&127)|128);n>>=7
   return bytes(out)+bytes([n])
  def b(k,v):return var(k*8+2)+var(len(v))+v
  import time
  stamp=int((time.time()+1)*1000);body=var(8)+var(1)+b(4,b(2,b'synthetic new text'));raw=var(8)+var(2)+b(2,b(2,b'peer'))+var(32)+var(stamp)+var(40)+var(1)+b(6,body)
  class Session:
   requests=1
   def get(self,*a,**kw):return json.dumps({'data':{'msgs':[{'msg':base64.b64encode(raw).decode()}],'is_last':True}})
  with patch('integrations.chaoxing_provider.runtime.reception.load_session',return_value=Session()),patch('integrations.chaoxing_provider.runtime.reception.NativeSources') as native:
   native.return_value.im_auth.return_value=('own','token');self.assertEqual(r.poll_one('private')['inserted'],1);self.assertEqual(r.poll_one('private')['inserted'],0)
  self.assertEqual(r.messages('private')['total'],1);r.close()
 def test_mutating_routes_not_allowed(self):
  self.assertFalse(approved_url('https://evil.example/'))
  self.assertFalse(approved_url('https://mooc1.chaoxing.com/work/submit'))
 def test_concurrent_collection_rejected(self):
  with state_lock(self.root):
   with self.assertRaises(SourceError):collect_once(self.root)
 @unittest.skipUnless(os.name=='nt','Windows DPAPI only')
 def test_encrypted_session_roundtrip_and_account_change(self):
  from .runtime.session import save_session
  from .runtime.transport import load_session
  cookies=[{'domain':'.chaoxing.com','name':'synthetic','value':'NOT-A-REAL-COOKIE','path':'/'}]
  with patch('integrations.chaoxing_provider.runtime.session.NativeSources') as native:
   native.return_value.im_auth.return_value=('account-A','synthetic')
   self.assertTrue(save_session(self.root,cookies)['authenticated'])
   raw=(self.root/'discovery-session.local.dpapi').read_bytes();self.assertNotIn(b'NOT-A-REAL-COOKIE',raw)
   self.assertTrue(load_session(self.root).account_hash)
   native.return_value.im_auth.return_value=('account-B','synthetic')
   with self.assertRaises(SourceError):save_session(self.root,cookies)
   self.assertEqual(raw,(self.root/'discovery-session.local.dpapi').read_bytes())
if __name__=='__main__':unittest.main()
