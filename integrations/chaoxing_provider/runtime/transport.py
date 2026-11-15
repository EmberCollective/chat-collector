"""Read-only, allowlisted network session. Secrets never enter logs or n8n."""
import http.cookiejar
import json
import os
import re
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from .vault import _crypt


class SourceError(Exception):
    def __init__(self,code,retry_after=60):self.code=code;self.retry_after=retry_after;super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def approved_url(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme!='https' or p.username or p.password or p.port not in (None,443):return False
    host=p.hostname or ''
    if host=='a3-vip6.easemob.com':
        return bool(re.fullmatch(r'/cx-dev/cxstudy/(?:users/[A-Za-z0-9_]+/(?:joined_chatgroups|messageroaming)|sdk/user/[A-Za-z0-9_]+/user_channels/list)',p.path))
    if host=='notice.chaoxing.com' and p.path=='/pc/notice/getNoticeList':return True
    if host=='i.chaoxing.com' and p.path=='/base':return True
    if not (host=='chaoxing.com' or host.endswith('.chaoxing.com')):return False
    allowed=('/apis/getLoginUser','/apis/user/getUserImToken','/webim/huanxin/getGroupInfo',
        '/v2/apis/active/student/activelist','/v2/apis/active/getPPTActiveInfo','/mycourse/backclazzdata',
        '/visit/interaction','/mooc2/work/list')
    allowed+=('/mooc-ans/visit/stucoursemiddle','/visit/stucoursemiddle','/mooc2-ans/mycourse/stu',
        '/mooc-ans/mycourse/stu','/exam-ans/mooc2/exam/exam-list')
    return p.path in allowed


class ReadSession:
    def __init__(self,cookies):
        self.jar=http.cookiejar.CookieJar();self.requests=0;self.bytes=0;self.elapsed=0;self.last_request=0
        for c in cookies:
            domain=c.get('domain','')
            if not (domain=='chaoxing.com' or domain.endswith('.chaoxing.com')):continue
            expiry=c.get('expires',0)
            self.jar.set_cookie(http.cookiejar.Cookie(0,c['name'],c['value'],None,False,domain,domain.startswith('.'),domain.startswith('.'),
                c.get('path','/'),True,c.get('secure',True),int(expiry) if expiry and expiry>0 else None,False,None,None,{},False))
        # Fixed direct transport for this program's allowlisted domestic APIs;
        # do not inherit the user's unrelated browser/system proxy.
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPCookieProcessor(self.jar))
    def get(self,url,params=None,bearer=None,history=None,_redirects=0):
        if params:url+='&' if '?' in url else '?';url+=urllib.parse.urlencode(params)
        if not approved_url(url):raise SourceError('unapproved_read_route',3600)
        time.sleep(max(0,0.4-(time.monotonic()-self.last_request)))
        self.last_request=time.monotonic()
        data=None
        if history is not None:
            import re
            if urllib.parse.urlsplit(url).hostname!='a3-vip6.easemob.com' or not re.fullmatch(r'/cx-dev/cxstudy/users/[A-Za-z0-9_]+/messageroaming',urllib.parse.urlsplit(url).path):raise SourceError('unapproved_read_route')
            if set(history)-{'queue','start','end','pull_number','is_positive','msgType','startTime','endTime','userId'} or not re.fullmatch(r'(?:[0-9]+@conference\.easemob\.com|[A-Za-z0-9_-]+@easemob\.com)',history.get('queue','')):raise SourceError('invalid_history_scope')
            data=json.dumps(history).encode()
        headers={'User-Agent':'Mozilla/5.0','Accept':'application/json,text/html;q=0.9','Referer':'https://fe.chaoxing.com/'}
        if data is not None:headers['Content-Type']='application/json'
        if bearer:
            if urllib.parse.urlsplit(url).hostname!='a3-vip6.easemob.com':raise SourceError('invalid_token_target')
            headers['Authorization']='Bearer '+bearer
        started=time.monotonic();self.requests+=1
        try:
            with self.opener.open(urllib.request.Request(url,data=data,headers=headers),timeout=12) as response:
                body=response.read(4_000_001);self.bytes+=len(body)
                if len(body)>4_000_000:raise SourceError('response_too_large',3600)
                return body.decode('utf-8','replace')
        except urllib.error.HTTPError as e:
            if e.code in (301,302,303,307,308):
                target=urllib.parse.urljoin(url,e.headers.get('Location',''))
                if not bearer and _redirects<3 and approved_url(target):return self.get(target,_redirects=_redirects+1)
                raise SourceError('needs_login',300) from None
            if e.code==401:raise SourceError('im_token_expired' if bearer else 'needs_login',60 if bearer else 300) from None
            if e.code in (403,429):
                delay=e.headers.get('Retry-After','300');raise SourceError('rate_limited' if e.code==429 else 'access_denied',min(1800,int(delay)) if delay.isdigit() else 300) from None
            raise SourceError('http_'+str(e.code),300) from None
        except (urllib.error.URLError,TimeoutError,ConnectionError,OSError):raise SourceError('network_unavailable',60) from None
        finally:self.elapsed+=time.monotonic()-started
    def json(self,url,params=None,bearer=None):
        raw=self.get(url,params,bearer)
        try:return json.loads(raw)
        except ValueError:raise SourceError('non_json_response',300) from None
    def history(self,uid,group,token,cursor=-1):
        raw=self.get('https://a3-vip6.easemob.com/cx-dev/cxstudy/users/'+str(uid)+'/messageroaming',bearer=token,
            history={'queue':str(group)+'@conference.easemob.com','start':cursor,'end':-1,'pull_number':50,'is_positive':False,'msgType':''})
        try:return json.loads(raw)
        except ValueError:raise SourceError('non_json_response',300) from None


def load_session(root):
    try:
        value=json.loads(_crypt((Path(root)/'discovery-session.local.dpapi').read_bytes(),False))
        if not isinstance(value,dict) or not isinstance(value.get('cookies'),list) or not value.get('account_hash'):raise ValueError()
        session=ReadSession(value['cookies']);session.account_hash=value['account_hash'];return session
    except (OSError,ValueError,KeyError,TypeError):raise SourceError('needs_session_bootstrap',300) from None
