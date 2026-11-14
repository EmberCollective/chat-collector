import os
from pathlib import Path

import pytest

import rules
from rules import discover_rules
from test_console import console


def relative(path, base):
    return Path(os.path.relpath(path, base)).as_posix()


def setup(tmp_path):
    root = tmp_path / 'project'
    (root / 'rulesets').mkdir(parents=True)
    base = root / 'config'
    base.mkdir()
    config = base / 'source.toml'
    config.touch()
    return root, base, config


def test_discovery_scope_aliases_new_rules_and_disabled_missing_files(tmp_path):
    root, base, config = setup(tmp_path)
    standard = root / 'rulesets' / 'keyword.sql'
    standard.write_text('SELECT identity FROM candidates WHERE text=:needle', encoding='utf-8')
    external = tmp_path / 'explicit'
    external.mkdir()
    (external / 'added.SQL').write_text('SELECT identity FROM candidates WHERE :recent_count>=0')
    missing = external / 'disabled-missing.sql'
    nested = root / 'rulesets' / 'nested'
    nested.mkdir()
    (nested / 'not-discovered.sql').write_text('SELECT identity FROM candidates')
    (root / 'not-in-scope.sql').write_text('SELECT identity FROM candidates')
    (external / 'private.txt').write_text('PRIVATE_CONTENT')
    registrations = [{'file': str(standard), 'params': {'needle': 'retain me'}},
                     {'file': '../rulesets/./keyword.sql', 'enabled': False},
                     {'file': str(missing), 'enabled': False}]
    result = discover_rules(root, config, {'filters': {'rulesets': registrations}})
    rows = {item['file']: item for item in result['rules']}
    assert set(rows) == {relative(standard, base), relative(missing, base), relative(external/'added.SQL', base)}
    row = rows[relative(standard, base)]
    assert row['registered'] and row['parameters'] == ['needle']
    assert row['registered_files'] == [str(standard), '../rulesets/./keyword.sql']
    assert not rows[relative(missing, base)]['exists'] and rows[relative(missing, base)]['registered']
    assert rows[relative(missing, base)]['error']
    added = rows[relative(external/'added.SQL', base)]
    assert not added['registered'] and added['recent_count']
    assert str(root) not in result['directories'] and str(external) in result['directories']
    assert 'PRIVATE_CONTENT' not in str(result)
    assert registrations[1]['enabled'] is False and len(registrations) == 3
    assert result == discover_rules(root, config, {'filters': {'rulesets': registrations}})


def test_discovery_rejects_link_escape_and_does_not_read_non_sql_targets(tmp_path):
    root, base, config = setup(tmp_path)
    private = tmp_path / 'private.txt'
    private.write_text('DO_NOT_RETURN_THIS')
    link = root / 'rulesets' / 'linked.sql'
    try:
        os.symlink(private, link)
    except OSError:
        pytest.skip('Symbolic links require Windows developer mode')
    result = discover_rules(root, config, {'filters': {'rulesets': [{'file': str(link), 'enabled': False}, {'file': str(private), 'enabled': False}]}})
    row = result['rules'][0]
    assert row['error'] and 'sql' not in row and row['registered']
    assert 'DO_NOT_RETURN_THIS' not in str(result) and str(private) not in str(result)
    assert result['warnings']


def test_default_directory_link_cannot_expand_discovery_outside_project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private.sql').write_text('PRIVATE_SQL')
    try:
        os.symlink(outside, root / 'rulesets', target_is_directory=True)
    except OSError:
        pytest.skip('Symbolic links require Windows developer mode')
    result = discover_rules(root, root/'source.toml', {'filters': {'rulesets': []}})
    assert result['rules'] == [] and result['directory_details'][0]['error']
    assert str(outside) not in str(result) and 'PRIVATE_SQL' not in str(result)


def test_directory_link_loop_is_reported_without_following_it(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    link = root / 'rulesets'
    try:
        os.symlink(link, link, target_is_directory=True)
    except OSError:
        pytest.skip('Symbolic links require Windows developer mode')
    result = discover_rules(root, root/'source.toml', {'filters': {}})
    assert result['rules'] == [] and result['directory_details'][0]['error']


def test_rule_and_content_limits_are_explicit_and_keep_registered_missing(tmp_path, monkeypatch):
    root, base, config = setup(tmp_path)
    monkeypatch.setattr(rules, 'MAX_RULES', 2)
    monkeypatch.setattr(rules, 'MAX_SQL_BYTES', 64)
    (root/'rulesets'/'a.sql').write_text('SELECT identity FROM candidates')
    (root/'rulesets'/'b.sql').write_text('X' * 65)
    (root/'rulesets'/'c.sql').write_text('SELECT identity FROM candidates')
    result = discover_rules(root, config, {'filters': {'rulesets': [{'file': '../rulesets/missing.sql', 'enabled': False}]}})
    assert len(result['rules']) == 2 and result['omitted']['rules'] == 2 and result['warnings']
    assert any(row['registered'] and not row['exists'] for row in result['rules'])
    monkeypatch.setattr(rules, 'MAX_RULES', 256)
    result = discover_rules(root, config, {'filters': {}})
    assert next(row for row in result['rules'] if row['file'].endswith('b.sql'))['error']
    monkeypatch.setattr(rules, 'MAX_TOTAL_SQL_BYTES', 32)
    result = discover_rules(root, config, {'filters': {}})
    assert result['omitted']['sql_bytes'] and sum(len(row.get('sql', '').encode()) for row in result['rules']) <= 32


def test_directory_and_registration_limits_do_not_hide_their_partial_scope(tmp_path, monkeypatch):
    root, base, config = setup(tmp_path)
    for file in ('a.sql', 'b.sql'):
        (root / 'rulesets' / file).write_text('SELECT identity FROM candidates')
    monkeypatch.setattr(rules, 'MAX_DIRECTORY_ENTRIES', 1)
    result = discover_rules(root, config, {'filters': {}})
    assert len(result['rules']) == 1 and result['omitted']['directory_entries']
    assert result['directory_details'][0]['error'] and result['rules'][0]['sql']
    monkeypatch.setattr(rules, 'MAX_REGISTERED', 1)
    result = discover_rules(root, config, {'filters': {'rulesets': [
        {'file': '../rulesets/first-missing.sql', 'enabled': False},
        {'file': '../rulesets/second-missing.sql', 'enabled': False}]}})
    assert result['omitted']['registered'] == 1 and result['warnings']
    assert any(row['file'].endswith('first-missing.sql') and row['registered'] for row in result['rules'])
    assert not any(row['file'].endswith('second-missing.sql') for row in result['rules'])


def test_authenticated_ruleset_route_guards_source_and_capability(console):
    client, app, _, _ = console
    original = app.state.bridge.paths['wechat'].read_bytes()
    response = client.get('/api/modules/wechat/rulesets')
    assert response.status_code == 200 and response.json()['rules']
    assert client.get('/api/modules/wechat/rulesets?directory=arbitrary-private-folder').json() == response.json()
    assert client.get('/api/modules/wechat/rulesets', headers={'X-Console-Token': ''}).status_code == 401
    assert client.get('/api/modules/unknown/rulesets').status_code == 400
    app.state.bridge.sources['outside'] = {'capabilities': {'sql': False}}
    assert client.get('/api/modules/outside/rulesets').status_code == 400
    assert app.state.bridge.paths['wechat'].read_bytes() == original
