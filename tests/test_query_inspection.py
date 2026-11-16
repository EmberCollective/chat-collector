import pytest

from providers import query


def test_inspection_collects_real_bindings_and_ignores_comments_and_quotes():
    result = query.inspect_rule('''SELECT identity AS "quoted:@ignored" FROM candidates
        WHERE text=':literal' AND text=:keyword AND kind=@kind
          AND sent_at>=:window_start AND sent_at<:window_end AND :recent_count>=0
        /* $not_a_parameter */ -- :also_ignored
    ''', 'custom.sql')
    assert result == {'parameters': ['keyword', 'kind'], 'recent_count': True, 'error': None}
    assert query.inspect_rule('SELECT identity FROM candidates WHERE :recent_count=:recent_count', 'context.sql')['recent_count']


def test_sqlite_aliases_and_tcl_binding_names_are_taken_from_the_compiler():
    result = query.inspect_rule('SELECT ?1, :term, ?2, $scope::part(suffix), @term', 'bindings.sql')
    assert result == {'parameters': ['1', 'scope::part(suffix)', 'term'], 'recent_count': False, 'error': None}
    partial = query.inspect_rule('SELECT :first, ?', 'positional.sql')
    assert partial['parameters'] == ['first'] and partial['error']
    reserved = query.inspect_rule('SELECT :_private', 'reserved.sql')
    assert reserved['parameters'] == ['_private'] and 'reserved' in reserved['error']


def test_explain_never_calls_functions_or_runs_the_submitted_select(monkeypatch):
    schema = query._schema
    def prepare(db):
        schema(db)
        db.create_function('must_not_execute', 1, lambda value: pytest.fail('SQL was executed'))
    monkeypatch.setattr(query, '_schema', prepare)
    result = query.inspect_rule('SELECT must_not_execute(:needle)', 'read.sql')
    assert result['parameters'] == ['needle'] and result['error'] is None


@pytest.mark.parametrize('sql', [
    'DELETE FROM messages', "ATTACH ':memory:' AS private", 'PRAGMA user_version=3',
    "SELECT load_extension('private')", 'SELECT * FROM sqlite_master', 'SELECT :missing FROM unknown_table',
])
def test_inspection_reuses_provider_read_only_authorization(sql):
    assert query.inspect_rule(sql, 'denied.sql')['error']


def test_inspection_bounds_collected_bindings():
    sql = 'SELECT ' + ','.join(':p' + str(number) for number in range(1001))
    result = query.inspect_rule(sql, 'many.sql')
    assert len(result['parameters']) == 1000 and 'limit' in result['error']
