"""Minimal IM source adapter; no coursework, browser, or UI dependency."""
import hashlib
from .transport import SourceError
IM='https://a3-vip6.easemob.com/cx-dev/cxstudy'
class NativeSources:
    def __init__(self,session):self.session=session;self.im=None
    def catalog(self):return {},True # names remain explicitly unmapped in portable v1
    def im_auth(self):
        data=self.session.json('https://learn.chaoxing.com/apis/user/getUserImToken')
        m=data.get('msg')
        if data.get('result') not in (1,True) or not isinstance(m,dict) or not m.get('hxToken') or not m.get('uid'):raise SourceError('needs_login',300)
        uid=str(m['uid'])
        if getattr(self.session,'account_hash',None) and hashlib.sha256(uid.encode()).hexdigest()!=self.session.account_hash:raise SourceError('account_changed_use_new_state',3600)
        self.im=(uid,m['hxToken']);return self.im
    def heads(self):
        uid,token=self.im or self.im_auth()
        raw=self.session.json(IM+'/sdk/user/'+uid+'/user_channels/list',{'limit':100,'need_mark':'true','needEmptySession':'false'},bearer=token)
        data=raw.get('data',{});rows=data.get('channel_infos')
        if not isinstance(rows,list):raise SourceError('invalid_conversations',300)
        return rows,not (raw.get('cursor') or data.get('cursor') or len(rows)>=100)
