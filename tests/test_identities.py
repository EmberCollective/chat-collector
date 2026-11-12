import json
from pathlib import Path
import sqlite3

from collector.identities import IdentityContext
from processor.context import prepare_input


def message(sender='alice',peer='room@chatroom'):
    return {'platform':'wechat','account_id':'self','conversation_id':peer,'conversation_type':'group',
            'message_id':'1','sender_id':sender,'sent_at':100,'text':'Submit tomorrow','attachments':[]}


def test_wechat_provider_emits_only_context_people_and_preserves_names(tmp_path):
    with sqlite3.connect(tmp_path/'contact.db') as db:
        db.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT)')
        db.executemany('INSERT INTO contact VALUES(?,?,?)',[
            ('self','My nickname',''),('alice','Alice','Teacher'),('unrelated','Private unrelated person','Secret'),
            ('room@chatroom','Study group','Group remark')])
    context=IdentityContext({'platform':'wechat','source':{'account_id':'self','decrypted_dir':str(tmp_path),'account_label':'Work'}})
    try:
        item=context.enrich(message())
        assert [p['id'] for p in item['identity']['people']]==['alice']
        assert item['identity']['people'][0]['display_name']=='Teacher'
        assert item['identity']['account']['nickname']=='My nickname'
        assert item['identity']['conversation']['name']=='Study group'
        assert 'Private unrelated person' not in json.dumps(item)
    finally:context.close()


def test_qq_names_come_from_actual_message_and_not_unrelated_members(tmp_path):
    with sqlite3.connect(tmp_path/'nt_msg.db') as db:
        db.execute('CREATE TABLE group_msg_table("40001" INTEGER PRIMARY KEY,"40033" INTEGER,"40020" TEXT,"40050" INTEGER,"40093" TEXT,"40090" TEXT)')
        db.executemany('INSERT INTO group_msg_table VALUES(?,?,?,?,?,?)',[(1,123,'uid-one',100,'Alice','Group Alice'),(2,456,'uid-other',101,'Secret','Secret card'),(3,9,'uid-me',99,'Myself','My card')])
        db.execute('CREATE TABLE recent_contact_v3_table("40010" INTEGER,"40021" TEXT,"40030" INTEGER,"40094" TEXT)')
        db.execute('INSERT INTO recent_contact_v3_table VALUES(2,"room",0,"Study group")')
    context=IdentityContext({'platform':'qq','source':{'account_id':'9','decrypted_dir':str(tmp_path)}})
    try:
        item=context.enrich(dict(message('123','room'),platform='qq',account_id='9'))
        assert len(item['identity']['people'])==1
        assert item['identity']['people'][0]['nickname']=='Alice'
        assert item['identity']['people'][0]['group_nickname']=='Group Alice'
        assert item['identity']['people'][0]['remark'] is None
        assert item['identity']['account']['nickname']=='Myself'
        assert context.person('123','another-room','group')['group_nickname'] is None
        assert item['identity']['conversation']['name']=='Study group'
        assert 'Secret' not in json.dumps(item)
    finally:context.close()


def test_model_people_are_scoped_and_raw_ids_are_not_sent(tmp_path):
    record=message()
    record['identity']={'account':{'id':'self','nickname':'Myself','label':'Work'},
        'conversation':{'id':'room@chatroom','name':'Study group','type':'group'},
        'people':[{'id':'alice','display_name':'Teacher','nickname':'Alice','remark':'Teacher','group_nickname':'Lead'},
                  {'id':'unrelated','display_name':'Must not enter context'}]}
    events=[{'message_key':'key','message':record}]
    group={'id':'group','platform':'wechat','account_id':'self','conversation_id':'room@chatroom',
           'messages':events,'primary_message_keys':['key']}
    cfg={'storage':{'state_file':str(tmp_path/'state.db')},'context':{'timezone':'Asia/Shanghai'}}
    payload,*_=prepare_input(cfg,[group])
    people=payload['conversations'][0]['people']
    assert len(people)==1 and people[0]['display_name']=='Teacher'
    assert payload['conversations'][0]['segments'][0]['messages'][0]['sender_ref']==people[0]['ref']
    assert 'Must not enter context' not in json.dumps(payload)
    assert 'room@chatroom' not in json.dumps(payload)
    assert payload['accounts'][0]['self']['nickname']=='Myself'


def test_historical_file_anchor_keeps_type_and_attachment_index(tmp_path):
    record=dict(message(),text='',kind='file',mentions_all=False,attachments=[{'type':'file','name':'notice.pdf'}])
    events=[{'message_key':'old-file','message':record},
            {'message_key':'new-reply','message':dict(message(),message_id='2',sent_at=200,kind='text')}]
    group={'id':'group','platform':'wechat','account_id':'self','conversation_id':'room@chatroom',
           'messages':events,'primary_message_keys':['new-reply']}
    cfg={'storage':{'state_file':str(tmp_path/'state.db')},'context':{'timezone':'Asia/Shanghai'}}
    payload,*_=prepare_input(cfg,[group])
    rows=payload['conversations'][0]['segments'][0]['messages']
    assert rows[0]['kind']=='file' and rows[0]['role']=='history' and rows[0]['attachments']==['f1']
    assert rows[1]['role']=='current'
    assert len(payload['conversations'][0]['people'])==1


def test_missing_identity_stays_explicit_and_does_not_invent_names(tmp_path):
    context=IdentityContext({'platform':'wechat','source':{'account_id':'self','decrypted_dir':str(tmp_path)}})
    try:
        result=context.enrich(message())
        assert result['identity']['people'][0]['display_name'] is None
        assert result['identity']['warnings']==['identity_database_missing']
    finally:context.close()


def test_identity_observation_changes_do_not_emit_but_name_changes_do(tmp_path):
    from collector.store import Store
    record=message()
    record['identity']={'people':[{'id':'alice','nickname':'Alice','observed_at':'first','source':'one'}]}
    store=Store(tmp_path/'events.db','wechat','self')
    try:
        assert store.ingest([record],0,200,'first')['emitted']==1
        record['identity']['people'][0].update(observed_at='second',source='two')
        assert store.ingest([record],0,200,'second')['emitted']==0
        record['identity']['people'][0]['nickname']='New name'
        assert store.ingest([record],0,200,'third')['emitted']==1
    finally:store.close()
