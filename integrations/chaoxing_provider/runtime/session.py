"""Explicit login bootstrap. Never stores or prints plaintext cookies."""
import base64,hashlib,json,os,subprocess
from pathlib import Path
from .transport import ReadSession,SourceError
from .sources import NativeSources
from .vault import _crypt

def private_directory(root):
 root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
 if os.name!='nt':raise SourceError('windows_dpapi_required',3600)
 # User identity is resolved by Windows, not guessed from USERNAME or a config file.
 script="$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; & icacls.exe $env:CHAOXING_STATE_DIR '/inheritance:r' '/grant:r' ('*'+$sid+':(OI)(CI)F') '*S-1-5-18:(OI)(CI)F' | Out-Null; if($LASTEXITCODE -ne 0){exit 1}"
 result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(script.encode('utf-16le')).decode()],env={**os.environ,'CHAOXING_STATE_DIR':str(root)},capture_output=True,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
 if result.returncode:raise SourceError('private_directory_acl_failed',3600)
 (root/'.initialized').write_text('chaoxing-runtime-v1',encoding='ascii')
 return root

def save_session(root,cookies):
 root=private_directory(root)
 if not isinstance(cookies,list):raise SourceError('invalid_cookies')
 filtered=[c for c in cookies if isinstance(c,dict) and (c.get('domain')=='chaoxing.com' or c.get('domain','').endswith('.chaoxing.com')) and isinstance(c.get('name'),str) and isinstance(c.get('value'),str)]
 if not filtered:raise SourceError('no_chaoxing_cookies')
 session=ReadSession(filtered);uid,_=NativeSources(session).im_auth();account=hashlib.sha256(uid.encode()).hexdigest();target=root/'discovery-session.local.dpapi'
 if target.exists():
  prior=json.loads(_crypt(target.read_bytes(),False))
  if prior.get('account_hash')!=account:raise SourceError('account_changed_use_new_state',3600)
 value={'account_hash':account,'cookies':filtered};tmp=target.with_suffix('.tmp');tmp.write_bytes(_crypt(json.dumps(value).encode(),True));os.replace(tmp,target)
 return {'session_saved':True,'authenticated':True,'password_stored':False}

def bootstrap_browser(root,port):
 if type(port) is not int or not 1024<=port<=65535:raise SourceError('invalid_cdp_port')
 from playwright.sync_api import sync_playwright
 # Caller explicitly selects a dedicated localhost browser. No launch, navigation, or closing tabs.
 with sync_playwright() as api:
  browser=api.chromium.connect_over_cdp('http://127.0.0.1:'+str(port),timeout=10000)
  if len(browser.contexts)!=1:raise SourceError('choose_browser_context_required')
  cookies=browser.contexts[0].cookies(['https://i.chaoxing.com/base','https://learn.chaoxing.com/','https://mooc1-api.chaoxing.com/'])
 return save_session(root,cookies)
