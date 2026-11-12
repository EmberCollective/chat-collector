"""Registered SQL predicates and conversation-local context selection."""
import importlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile


class RuleRegistry:
    def __init__(self):
        self.builders = {}

    def register(self, name, builder):
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', name):
            raise ValueError('Invalid filter type name')
        if name in self.builders or not callable(builder):
            raise ValueError('Duplicate filter type or non-callable builder: '+name)
        self.builders[name] = builder


def fields(rule, required=(), optional=()):
    if set(rule)-{'type', *required, *optional} or not set(required)<=set(rule):
        raise ValueError('Invalid fields for filter '+str(rule.get('type')))


def _values(rule):
    fields(rule, ('values',))
    values = rule['values']
    if not isinstance(values, list) or not values or any(not isinstance(x,str) or not x for x in values):
        raise ValueError('Filter values must be a nonempty array of nonempty strings')
    return values


def _keywords(rule, compiler):
    return '('+' OR '.join(f'instr(lower(text),lower({compiler.bind(v)}))>0' for v in _values(rule))+')'


def _membership(column):
    def build(rule, compiler):
        return column+' IN ('+','.join(compiler.bind(v) for v in _values(rule))+')'
    return build


def _mentions(rule, compiler):
    fields(rule)
    return 'mentions_all=1'


def _combine(rule, compiler):
    fields(rule, ('conditions',))
    conditions = rule['conditions']
    if not isinstance(conditions,list) or not conditions:
        raise ValueError('Combined filters need nonempty conditions')
    return '('+(' AND ' if rule['type']=='all' else ' OR ').join('('+compiler.compile(c)+')' for c in conditions)+')'


def _nearby(rule, compiler):
    fields(rule, ('condition',), ('before','after'))
    before, after = rule.get('before',1), rule.get('after',1)
    for value in (before,after):
        if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=1000:
            raise ValueError('nearby before/after must be integers from 0 to 1000')
    condition = compiler.compile(rule['condition'])
    compiler.before += before
    compiler.after += after
    if max(compiler.before,compiler.after)>1000:
        raise ValueError('Total context span per side cannot exceed 1000')
    alias = '_rule_window_'+str(len(compiler.windows))
    expression = f'''max(CASE WHEN ({condition}) THEN 1 ELSE 0 END) OVER (
        PARTITION BY conversation_type,conversation_id
        ORDER BY sent_at,CAST(local_id AS INTEGER),database_name,message_id
        ROWS BETWEEN {before} PRECEDING AND {after} FOLLOWING)'''
    compiler.windows.append((alias,expression))
    return alias+'=1'


class SqlRules:
    """Builders return SQL; bind() parameterizes values; compile() composes children."""
    def __init__(self, register=None, prefix='_rule_p'):
        self.registry = RuleRegistry()
        for name,builder in {'keywords':_keywords,'kind':_membership('kind'),
                             'conversation':_membership('conversation_id'),'sender':_membership('sender_id'),
                             'mentions_all':_mentions,'all':_combine,'any':_combine,'nearby':_nearby}.items():
            self.registry.register(name,builder)
        if register:
            register(self.registry)
        self.params, self.windows = {}, []
        self.prefix = prefix
        self.before = self.after = self.depth = self.nodes = 0

    def bind(self, value):
        if value is not None and not isinstance(value,(str,int,float,bytes)):
            raise ValueError('SQL filter parameter must be a scalar')
        key = self.prefix+str(len(self.params))
        self.params[key] = value
        return ':'+key

    def compile(self, rule):
        self.depth += 1
        self.nodes += 1
        try:
            if self.depth>8 or self.nodes>128:
                raise ValueError('Filter tree exceeds 8 levels or 128 nodes')
            if not isinstance(rule,dict) or not isinstance(rule.get('type'),str) or rule['type'] not in self.registry.builders:
                raise ValueError('Unknown filter type')
            sql = self.registry.builders[rule['type']](rule,self)
            if not isinstance(sql,str) or not sql.strip():
                raise ValueError('Filter builder must return a SQL expression')
            return sql
        finally:
            self.depth -= 1


def compile_rules(config):
    from .config import load_filter_extension
    settings = config.get('filters',{})
    rules = settings.get('rules',[])
    if not isinstance(rules,list):
        raise ValueError('filters.rules must be an array')
    module = load_filter_extension(settings) if rules and settings.get('code_file') else None
    compiler = SqlRules(getattr(module,'register_filters',None))
    predicate = ' AND '.join('('+compiler.compile(rule)+')' for rule in rules) or '1'
    return compiler,predicate


def scan_filtered(config, start, end):
    """Keep native timestamp filtering; context affects eligibility, not checkpoints."""
    from .config import build_filter
    adapter = importlib.import_module('collector.adapters.'+config['platform'])
    where, params = build_filter(config)
    compiler,predicate = compile_rules(config)
    if not compiler.windows:
        yield from adapter.scan(config['source'],start,end,f'({where}) AND ({predicate})',dict(params,**compiler.params))
        return
    with tempfile.TemporaryDirectory(prefix='collector-filter-') as folder:
        db = sqlite3.connect(Path(folder)/'context.db')
        try:
            db.execute('''CREATE TABLE messages(identity TEXT PRIMARY KEY,conversation_id TEXT,
                conversation_type TEXT,sender_id TEXT,sent_at INTEGER,message_id TEXT,local_id TEXT,
                database_name TEXT,text TEXT,kind TEXT,mentions_all INTEGER,eligible INTEGER,body TEXT)''')
            conversations = set()
            def insert(message, eligible):
                source = message.get('source',{})
                identity = json.dumps([message.get(k) for k in ('platform','account_id','conversation_id','message_id')])
                db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identity,message['conversation_id'],message['conversation_type'],message['sender_id'],
                     message['sent_at'],message['message_id'],str(source.get('local_id','0')),
                     Path(source.get('database','')).name,message['text'],message['kind'],
                     int(message['mentions_all']),eligible,json.dumps(message,ensure_ascii=False)))
            for message in adapter.scan(config['source'],start,end,where,params):
                insert(message,1)
                conversations.add((message['conversation_type'],message['conversation_id']))
            if not conversations:
                return
            # Unfiltered neighbors must retain their positions even when they fail base filters.
            for message in adapter.scan(config['source'],start,end,context=(compiler.before,compiler.after),conversations=conversations):
                insert(message,0)
            db.commit()
            previous = 'messages'
            stages = []
            for index,(alias,expression) in enumerate(compiler.windows):
                name = '_stage_'+str(index)
                stages.append(f'{name} AS (SELECT *,{expression} AS {alias} FROM {previous})')
                previous = name
            query = 'WITH '+','.join(stages)+f''' SELECT body FROM {previous}
                WHERE eligible=1 AND ({predicate})
                ORDER BY sent_at,CAST(local_id AS INTEGER),database_name,message_id'''
            for row in db.execute(query,compiler.params):
                yield json.loads(row[0])
        finally:
            db.close()
