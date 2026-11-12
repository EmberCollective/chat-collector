import copy
import json
from pathlib import Path
import sqlite3

import pytest

from processor.task_search import TaskLookup, search_tasks


def task(identifier, *, platform='qq', conversation='room', status='open', deadline='2026-11-13', **extra):
    origin = {'platform':platform,'account_id':'private-account','conversation_id':conversation}
    return {'task_id':identifier,'revision':1,'title':'提交方案','event':'提交 API 方案 100%_完整',
        'priority':'P2','status':status,'conversation':origin,
        'time':{'start':None,'deadline':deadline,'text':None},
        'people':[{'name':'张老师','role':'负责人'},{'name':'李明','role':'协助'}],
        'location':None,'needs_review':False,'updated_at':'2026-11-12T10:00:00Z',
        'source_message_keys':['old-message'],
        'sources':[dict(origin,message_key='old-message',sender_id='private-sender',sent_at=1794489581,
            source={'database':'PRIVATE/raw.db'})],**extra}


def save(path, *tasks):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, revision INTEGER, body TEXT)')
        db.executemany('INSERT OR REPLACE INTO tasks VALUES(?,?,?)',[(t['task_id'],t['revision'],json.dumps(t)) for t in tasks])


@pytest.fixture
def config(tmp_path):
    return {'storage':{'state_file':str(tmp_path/'state.db')},'context':{'timezone':'Asia/Shanghai'}}


def test_full_database_filters_old_tasks_and_literal_keywords(config):
    path=config['storage']['state_file']
    target=task('old',platform='wechat',updated_at='2020-01-01T00:00:00Z')
    save(path,target, *[task(str(i),people=[{'name':'其他人','role':'负责人'}]) for i in range(60)])
    result=search_tasks(config,platforms=['wechat'],people=['张'],person_role='负责人',query='api',
                        priorities=['P2'],needs_review=False,statuses=['open'],time_from='2026-11-13',time_to='2026-11-14')
    assert [t['task_id'] for t in result['tasks']]==['old']
    assert not search_tasks(config,people=['张'],person_role='协助')['tasks']
    assert len(search_tasks(config,query='100%_',limit=100)['tasks'])==61
    assert not search_tasks(config,query="' OR 1=1 --")['tasks']
    assert not search_tasks(config,query='PRIVATE')['tasks']


def test_time_boundaries_offsets_nulls_and_source_time(config):
    save(config['storage']['state_file'],task('date'),task('instant',deadline='2026-11-13T00:00:00+08:00'),
         task('before',deadline='2026-11-12T15:59:59Z'),task('end',deadline='2026-11-14'),task('null',deadline=None))
    result=search_tasks(config,time_from='2026-11-12T16:00:00Z',time_to='2026-11-13T16:00:00Z')
    assert {t['task_id'] for t in result['tasks']}=={'date','instant'}
    assert len(search_tasks(config,time_field='source_time',time_from='2026-11-12',time_to='2026-11-13')['tasks'])==5
    assert not search_tasks(config,time_field='start',time_from='2026-11-12')['tasks']
    assert search_tasks(config,sort_by='deadline',order='asc')['tasks'][-1]['task_id']=='null'


def test_pagination_and_statuses_do_not_hide_remaining_results(config):
    save(config['storage']['state_file'],*[task(f'{i:03}') for i in range(45)],task('retired',status='superseded'))
    found=[]; offset=0
    while True:
        result=search_tasks(config,offset=offset,limit=20)
        found.extend(t['task_id'] for t in result['tasks'])
        if not result['has_more']:
            assert result['next_offset'] is None
            break
        offset=result['next_offset']
    assert len(found)==len(set(found))==45
    assert search_tasks(config,statuses=['superseded'])['tasks'][0]['task_id']=='retired'


@pytest.mark.parametrize('filters',[
    {'limit':0},{'limit':True},{'offset':-1},{'statuses':['anything']},{'priorities':['P9']},
    {'people':[]},{'platforms':'qq'},{'query':''},{'time_field':'SQL'},
    {'time_from':'2026-11-12T00:00:00'},{'time_from':'2026-11-13','time_to':'2026-11-12'},
    {'time_to':'yesterday'},{'order':'DROP TABLE tasks'},{'needs_review':'true'}])
def test_invalid_parameters_fail_even_when_database_missing(config,filters):
    with pytest.raises(ValueError):search_tasks(config,**filters)
    assert not Path(config['storage']['state_file']).exists()


def test_origin_filters_match_same_origin_and_support_cross_platform_evidence(config):
    item=task('cross')
    secondary=dict(item['sources'][0],collector_module='wechat',account_id='second',conversation_id='other')
    secondary.pop('platform')
    item['sources'].append(secondary)
    save(config['storage']['state_file'],item)
    assert search_tasks(config,platforms=['wechat'],account_id='second',conversation_id='other')['tasks']
    assert not search_tasks(config,platforms=['qq'],account_id='second')['tasks']


def test_debug_search_overlays_local_changes_without_writing_formal(config,tmp_path):
    formal=tmp_path/'formal.db'
    save(formal,task('shared'),task('only-formal'))
    config['_task_search_state_file']=str(formal)
    before=formal.read_bytes()
    assert len(search_tasks(config)['tasks'])==2
    assert not Path(config['storage']['state_file']).exists()
    save(config['storage']['state_file'],task('shared',title='本轮更新',revision=2),task('local'))
    result=search_tasks(config)
    assert len(result['tasks'])==3
    assert next(t for t in result['tasks'] if t['task_id']=='shared')['title']=='本轮更新'
    assert formal.read_bytes()==before


def test_dynamic_short_refs_are_stable_and_local_details_are_hidden(config):
    item=task('real-private-task',platform='wechat',conversation='private-group')
    save(config['storage']['state_file'],item)
    group={'platform':'qq','account_id':'private-account','conversation_id':'room'}
    mapping={}
    lookup=TaskLookup(config,[group],mapping)
    result=lookup.find(platforms=['wechat'])
    row=result['tasks'][0]
    assert row['task_id']=='t1' and row['conversation_ref']=='c2' and row['account_ref']=='a2'
    assert lookup.find(conversation_ref='c2')['tasks'][0]['task_id']=='t1'
    assert lookup.find(account_ref='a2')['tasks'][0]['task_id']=='t1'
    assert mapping['t1']==item
    assert all(v not in json.dumps(result) for v in ['private-account','private-group','real-private-task','PRIVATE','old-message'])
    with pytest.raises(ValueError,match='unknown_conversation'):lookup.find(conversation_ref='c999')
    with pytest.raises(ValueError,match='conflict'):lookup.find(conversation_ref='c2',platforms=['qq'])
