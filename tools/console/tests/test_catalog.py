"""Source identity is the configured instance, never a platform label."""
import pytest
import tomlkit

from bridge import Bridge
from test_console import console


def rewrite_settings(app, change):
    path = app.state.bridge.settings_path
    settings = tomlkit.parse(path.read_text(encoding='utf-8'))
    change(settings)
    path.write_text(tomlkit.dumps(settings), encoding='utf-8')
    return Bridge(path)


def test_named_instances_and_external_descriptions_keep_capabilities_honest(console):
    _, app, _, now = console
    def change(settings):
        settings['sources'] = {
            'wechat-work': {'kind': 'wechat', 'name': '工作微信', 'config': settings['modules']['wechat']},
            'qq-personal': {'kind': 'qq', 'name': '个人 QQ', 'config': settings['modules']['qq']},
            'outside': {'kind': 'future-provider', 'name': '外部收集器', 'description': '标准事件来源'},
        }
        del settings['modules']
    bridge = rewrite_settings(app, change)
    sources = {s['id']: s for s in bridge.catalog()['sources']}
    assert set(bridge.paths) == {'wechat-work', 'qq-personal'}
    assert sources['wechat-work']['kind'] == 'wechat'
    assert sources['wechat-work']['name'] == '工作微信'
    assert all(sources['wechat-work']['capabilities'].values())
    assert sources['outside']['readonly'] and not any(sources['outside']['capabilities'].values())
    assert sources['outside']['fields'] == []
    assert bridge.source_instance('wechat', 'fixture') == 'wechat-work'
    assert bridge.messages('wechat-work')[0]['message']['platform'] == 'wechat'
    assert bridge.execute('wechat-work', 'preview', {'start': now-60, 'end': now})['matched'] == 1
    for name in ('outside', 'missing', '../wechat-work'):
        for operation in (lambda: bridge.messages(name), lambda: bridge.read_config(name),
                          lambda: bridge.asset(name, 1, 'file.txt'),
                          lambda: bridge.execute(name, 'collect', {})):
            with pytest.raises(ValueError):
                operation()
    with pytest.raises(ValueError, match='未知采集操作'):
        bridge.execute('wechat-work', 'invalid', {})


def test_same_kind_instances_keep_state_and_evidence_identity_separate(console):
    _, app, root, _ = console
    duplicate = tomlkit.parse(app.state.bridge.paths['wechat'].read_text(encoding='utf-8'))
    duplicate['source']['account_id'] = 'work-account'
    duplicate['storage']['state_file'] = str(root/'second-state.db')
    config = root/'second-config.toml'
    config.write_text(tomlkit.dumps(duplicate), encoding='utf-8')
    def change(settings):
        settings['sources'] = {'wechat-work': {'kind': 'wechat', 'config': str(config), 'name': '工作账号'}}
    bridge = rewrite_settings(app, change)
    assert bridge.source_instance('wechat') is None
    assert bridge.source_instance('wechat', 'fixture') == 'wechat'
    assert bridge.source_instance('wechat', 'work-account') == 'wechat-work'
    assert bridge.messages('wechat-work') == []
    assert bridge.messages('wechat')
    assert not (root/'second-state.db').exists()


@pytest.mark.parametrize('entry', [
    {'id/invalid': {'kind': 'outside'}}, {'processor': {'kind': 'outside'}},
    {'outside': {'kind': 'outside', 'python': 'arbitrary.py'}},
    {'outside': {'kind': 'wechat'}}, {'outside': {'name': 'no kind or config'}},
    {'outside': {'kind': ''}},
])
def test_invalid_descriptions_are_rejected_without_dynamic_loading(console, entry):
    _, app, _, _ = console
    def change(settings): settings['sources'] = entry
    with pytest.raises(ValueError):
        rewrite_settings(app, change)


def test_duplicate_source_config_cannot_bypass_instance_identity(console):
    _, app, _, _ = console
    def change(settings):
        settings['sources'] = {'copy': {'kind': 'wechat', 'config': settings['modules']['wechat']}}
    with pytest.raises(ValueError, match='不能共享'):
        rewrite_settings(app, change)


def test_builtin_kind_must_match_registered_collector_configuration(console):
    _, app, _, _ = console
    def change(settings):
        settings['sources'] = {'wrong': {'kind': 'wechat', 'config': settings['modules']['qq']}}
    with pytest.raises(ValueError, match='类型与配置平台不匹配'):
        rewrite_settings(app, change)
