"""Provider-owned identity snapshots, restricted to people in selected messages."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
import sqlite3

from collector.vendor.wechat.contact_names import _load_group_nickname_map_from_contact_db


def _text(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    return value.strip() or None if isinstance(value, str) else None


class IdentityContext:
    def __init__(self, config):
        self.platform = config['platform']
        self.source = config['source']
        self.account = str(self.source['account_id'])
        folder = Path(self.source.get('decrypted_dir', '.'))
        self.path = folder/'contact.db' if self.platform == 'wechat' else Path(self.source.get('database') or folder/'nt_msg.db')
        self.conn = None
        self.columns, self.cache = {}, {}
        self.observed_at = None
        self.warning = None
        if self.path.is_file():
            try:
                self.conn = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True)
                self.conn.row_factory = sqlite3.Row
                self.conn.execute('PRAGMA query_only=ON')
                self.observed_at = dt.datetime.fromtimestamp(self.path.stat().st_mtime, dt.timezone.utc).isoformat()
                for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                    name = row[0]
                    self.columns[name] = {r[1] for r in self.conn.execute('PRAGMA table_info("'+name.replace('"','""')+'")')}
            except sqlite3.Error:
                self.warning = 'identity_database_unavailable'
                self.close()
        else:
            self.warning = 'identity_database_missing'

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _wechat(self, person, conversation):
        result = {'nickname':None,'remark':None,'group_nickname':None}
        if self.conn is not None:
            for table in ('contact','stranger'):
                if {'username','nick_name','remark'} <= self.columns.get(table,set()):
                    row = self.conn.execute(f'SELECT nick_name,remark FROM {table} WHERE username=? LIMIT 1',(person,)).fetchone()
                    if row:
                        result.update(nickname=_text(row[0]),remark=_text(row[1]))
                        break
            if conversation.endswith('@chatroom'):
                result['group_nickname'] = _load_group_nickname_map_from_contact_db(self.path,conversation,[person]).get(person)
        return result

    def _qq(self, person, conversation, kind, message_id=None):
        result = {'nickname':None,'remark':None,'group_nickname':None}
        table = 'group_msg_table' if kind=='group' else 'c2c_msg_table'
        columns = self.columns.get(table,set())
        if self.conn is None or not {'40001','40033','40093'} <= columns:
            return result
        card = '"40090"' if '40090' in columns and kind=='group' else 'NULL'
        sender = 'COALESCE(CAST(NULLIF("40033",0) AS TEXT),"40020")' if '40020' in columns else 'CAST("40033" AS TEXT)'
        if message_id is not None:
            row = self.conn.execute(f'SELECT "40093",{card} FROM {table} WHERE "40001"=? AND {sender}=?',(message_id,person)).fetchone()
        else:
            # Bounded fallback for own nickname; never export the surrounding messages.
            row = self.conn.execute(f'SELECT "40093",{card} FROM (SELECT * FROM {table} ORDER BY "40050" DESC LIMIT 5000) WHERE {sender}=? LIMIT 1',(person,)).fetchone() if '40050' in columns else None
        if row:
            if kind=='group':
                # A fallback from another group cannot establish this group's card.
                result.update(nickname=_text(row[0]),group_nickname=_text(row[1]) if message_id is not None else None)
            else:
                # QQ private-message field 40093 does not distinguish nickname from remark.
                result['display_name'] = _text(row[0])
        return result

    def person(self, identifier, conversation, kind, message_id=None):
        key = (identifier,conversation,kind,message_id)
        if key not in self.cache:
            try:
                names = self._wechat(identifier,conversation) if self.platform=='wechat' else self._qq(identifier,conversation,kind,message_id)
            except sqlite3.Error:
                names = {'nickname':None,'remark':None,'group_nickname':None}
                self.warning = 'identity_schema_unavailable'
            display = names.get('remark') or names.get('group_nickname') or names.get('nickname') or names.get('display_name')
            extra = [f'{label}：{names[key]}' for key,label in [('nickname','昵称'),('remark','备注'),('group_nickname','群昵称')]
                     if names.get(key) and names[key] != display]
            self.cache[key] = dict(names, id=identifier, display_name=display,
                label=(display or '名称未获取')+('（'+'；'.join(extra)+'）' if extra else ''),
                is_self=identifier==self.account, missing_fields=[k for k in ('nickname','remark','group_nickname') if not names.get(k)],
                source=self.path.name, observed_at=self.observed_at)
        return dict(self.cache[key])

    def enrich(self, message):
        conversation, kind = message['conversation_id'], message.get('conversation_type','direct')
        sender = message.get('sender_id','')
        own = self.person(self.account,'','group')
        account = {'id':self.account,'nickname':own.get('nickname'), 'label':self.source.get('account_label'),
                   'source':own['source'],'observed_at':own['observed_at']}
        if self.platform=='wechat':
            names = self.person(conversation,'',kind)
            chat = {'id':conversation,'type':kind,'name':names.get('nickname'),'remark':names.get('remark')}
        else:
            chat = {'id':conversation,'type':kind,'name':None,'remark':None}
            if self.conn is not None and {'40010','40021','40030','40094'} <= self.columns.get('recent_contact_v3_table',set()):
                row = self.conn.execute('SELECT "40094" FROM recent_contact_v3_table WHERE "40010"=? AND COALESCE(CAST(NULLIF("40030",0) AS TEXT),"40021")=? LIMIT 1',(2 if kind=='group' else 1,conversation)).fetchone()
                if row:
                    chat['name'] = _text(row[0])
        chat.update(display_name=chat['remark'] or chat['name'], source=self.path.name,observed_at=self.observed_at,
                    missing_fields=[k for k in ('name','remark') if not chat.get(k)])
        ids = list(dict.fromkeys([sender,*message.get('mentioned_user_ids',[]),message.get('reply_sender_id')]))
        if kind=='direct':
            ids.append(conversation)
        people = [self.person(person,conversation,kind,message['message_id'] if person==sender else None)
                  for person in dict.fromkeys(ids) if isinstance(person,str) and person]
        identity = {'account':account,'conversation':chat,'people':people}
        if self.warning:
            identity['warnings'] = [self.warning]
        sender_label = next((p['label'] for p in people if p['id']==sender),'名称未获取')
        return dict(message,identity=identity,sender=sender_label)
