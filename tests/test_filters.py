import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from collector.filters import compile_rules, scan_filtered
from collector.__main__ import run_round


@pytest.fixture(params=['wechat','qq'])
def config(request,tmp_path):
    platform = request.param
    plain = tmp_path/'plain'
    plain.mkdir()
    rows = [(1,90,'anchor',False),(2,100,'plain',False),(3,100,'document',True),
            (4,101,'plain',False),(5,102,'anchor',False),(6,110,'tail',False),(7,120,'tail',False)]
    if platform=='wechat':
        for shard,selected in enumerate((rows[:3],rows[3:])):
            with sqlite3.connect(plain/f'message_{shard}.db') as db:
                db.execute('CREATE TABLE Name2Id(user_name TEXT)')
                db.executemany('INSERT INTO Name2Id VALUES(?)',[('a@chatroom',),('sender',),('b@chatroom',)])
                for peer in ('a@chatroom','b@chatroom'):
                    table = 'Msg_'+hashlib.md5(peer.encode()).hexdigest()
                    db.execute(f'''CREATE TABLE "{table}"(local_id INTEGER PRIMARY KEY,server_id INTEGER,
                        local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,source BLOB,
                        message_content BLOB,compress_content BLOB)''')
                    data = selected if peer=='a@chatroom' else [(20+shard,100+shard,'unrelated',False)]
                    for identifier,stamp,text,file in data:
                        body = '<msg><appmsg><title>document</title><type>6</type></appmsg></msg>' if file else text
                        db.execute(f'INSERT INTO "{table}" VALUES(?,?,?,2,?,NULL,?,NULL)',(identifier,identifier+1000,(6<<32)|49 if file else 1,stamp,body))
    else:
        from collector.adapters.qq_vendor.c2c_40800_pb2 import MsgBody
        with sqlite3.connect(plain/'nt_msg.db') as db:
            db.execute('''CREATE TABLE group_msg_table("40001" INTEGER PRIMARY KEY,"40050" INTEGER,
                "40030" INTEGER,"40021" TEXT,"40033" INTEGER,"40020" TEXT,"40011" INTEGER,"40800" BLOB)''')
            for peer,selected in ((123,rows),(456,[(20,100,'unrelated',False),(21,101,'unrelated',False)])):
                for identifier,stamp,text,file in selected:
                    body = MsgBody()
                    item = body.content.add(content_type=3 if file else 1,text=text)
                    if file:
                        item.filename='document'
                    db.execute('INSERT INTO group_msg_table VALUES(?,?,?,?,?,?,?,?)',
                               (identifier,stamp,peer,str(peer),789,'sender',1,body.SerializeToString()))
    return {'platform':platform,'source':{'account_id':'account','decrypted_dir':str(plain),'refresh':False},
            'storage':{'state_file':str(tmp_path/'state.db'),'output_dir':str(tmp_path/'batches')},
            'schedule':{'initial_lookback_seconds':100},'filters':{}}


def nearby(before=1,after=0,condition=None):
    return {'type':'nearby','before':before,'after':after,'condition':condition or {'type':'keywords','values':['anchor']}}


def selected(config,start=100,end=111):
    return [int(m['source']['local_id']) for m in scan_filtered(config,start,end)]


def test_cross_window_conversation_and_shards(config):
    config['filters']['rules'] = [nearby()]
    assert selected(config)==[2,5,6]
    config['filters']['rules'] = [nearby(0,1)]
    assert selected(config)==[4,5]
    config['filters']['rules'] = [nearby(0,2)]
    # Equal timestamps use numeric local IDs; only in-window messages are emitted.
    assert selected(config,100,101)==[3]


def test_base_filter_does_not_remove_nonmatching_neighbors(config):
    config['filters'] = {'files':True,'rules':[nearby(0,2)]}
    assert selected(config)==[3]
    config['filters']['rules'] = [nearby(1,0)]
    assert selected(config)==[]


def test_overlap_is_unique_and_replay_deduplicates(config):
    config['filters']['rules'] = [nearby(2,2)]
    assert selected(config,90,121)==[1,2,3,4,5,6,7]
    first = run_round(config,start=90,end=121)
    second = run_round(config,start=90,end=121)
    assert (first['matched'],first['emitted'],second['duplicates'])==(7,7,7)
    with sqlite3.connect(config['storage']['state_file']) as db:
        assert db.execute("SELECT value FROM settings WHERE key='window_end'").fetchone() is None


def test_nested_and_combined_filters(config):
    config['filters']['rules'] = [nearby(1,0,nearby(1,0))]
    assert selected(config,100,101)==[2,3]
    config['filters']['rules'] = [{'type':'all','conditions':[nearby(0,2),{'type':'kind','values':['file']}]}]
    assert selected(config)==[3]
    config['filters']['rules'] = [nearby(0,0)]
    assert selected(config)==[5]


def test_registered_kind_can_be_used_inside_context_without_adapter_changes(config,tmp_path):
    config['filters']['code_file'] = str(Path(__file__).resolve().parents[1]/'examples/registered-filter.py')
    config['filters']['rules'] = [nearby(1,0,{'type':'text_length','minimum':8})]
    before = {p:p.read_bytes() for p in Path(config['source']['decrypted_dir']).glob('*.db')}
    assert selected(config)==[3,20,4,21]
    assert all(p.read_bytes()==data for p,data in before.items())


@pytest.mark.parametrize('rules', [None,{},[{'type':'missing'}],[{'type':'nearby','condition':{},'before':-1}],
    [nearby(True,0)],[nearby(1001,0)],[{'type':'keywords','values':[]}],
    [{'type':'keywords','values':['a'],'typo':1}]])
def test_bad_rules_fail_before_selection(rules):
    with pytest.raises(ValueError):
        compile_rules({'filters':{'rules':rules}})


def test_keywords_remain_parameterized(config):
    config['filters']['rules'] = [{'type':'keywords','values':["' OR 1=1 --"]}]
    assert selected(config)==[]
