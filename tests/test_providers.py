import hashlib
import json
from pathlib import Path

import pytest

from providers.attachments import AttachmentContext
from providers.registry import configure_providers


@pytest.fixture
def external(tmp_path):
    archive=tmp_path/'archive';archive.mkdir()
    file=archive/'notice.txt';file.write_text('Send the report by Friday.',encoding='utf-8')
    settings=tmp_path/'provider.toml'
    settings.write_text('platform="demo"\naccount_id="self"\n[files]\nnotice="archive/notice.txt"\n',encoding='utf-8')
    config={'storage':{'state_file':str(tmp_path/'state.db')},'attachments':{
        'archive_roots':[str(archive)],'cache_dir':str(tmp_path/'cache'),
        'providers':{'demo':{'factory':'examples.local_file_provider:LocalFileProvider','config':str(settings)}}}}
    event={'message_key':'m','message':{'platform':'demo','account_id':'self','attachments':[
        {'name':'notice.txt','type':'file','provider_ref':'notice'}]}}
    return config,event,file


def test_external_provider_is_lazy_and_tools_accept_only_short_refs(external):
    config,event,file=external
    reader=AttachmentContext([event],config)
    assert reader.catalog()[0]['status']=='resolvable'
    assert not reader.resolver.instances
    assert reader.read_attachment(str(file))['details']['status']=='needs_review'
    result=reader.read_attachment('f1')
    assert result['details']['status']=='ok'
    assert 'Send the report by Friday.' in json.dumps(result)
    assert str(file) not in json.dumps(result)
    assert reader.evidence['f1:b1']['message_keys']==['m']
    assert reader.resolved[('m',0)]['sha256']==hashlib.sha256(file.read_bytes()).hexdigest()


def test_external_provider_ref_cannot_be_a_path_and_wrong_account_is_rejected(external):
    config,event,file=external
    event['message']['attachments'][0]['provider_ref']=str(file)
    assert AttachmentContext([event],config).read_attachment('f1')['details']['warnings']==['unknown_provider_attachment']
    event['message']['account_id']='wrong'
    assert AttachmentContext([event],config).read_attachment('f1')['details']['warnings']==['attachment_account_mismatch']


def test_plugin_result_cannot_bypass_archive_roots(external):
    config,event,file=external
    config['attachments']['archive_roots']=[]
    result=AttachmentContext([event],config).read_attachment('f1')
    assert result['details']['warnings']==['attachment_outside_archive_roots']


def test_missing_plugin_is_a_tool_failure_not_a_crash(external):
    config,event,file=external
    config['attachments']['providers']['demo']['factory']='missing_test_provider:Factory'
    result=AttachmentContext([event],config).read_attachment('f1')
    assert result['details']['warnings']==['attachment_provider_unavailable']


def test_config_paths_are_resolved_without_importing_plugins(tmp_path):
    settings={'providers':{'demo':{'factory':'missing_test_provider:Factory','config':'provider.toml'}}}
    configure_providers(settings,tmp_path)
    assert settings['providers']['demo']['config']==str(tmp_path/'provider.toml')
    settings['collector_configs']={'demo':'other.toml'}
    with pytest.raises(ValueError,match='twice'):
        configure_providers(settings,tmp_path)


def test_provider_cannot_replace_the_requested_asset(external, monkeypatch):
    from examples.local_file_provider import LocalFileProvider
    config,event,file=external
    monkeypatch.setattr(LocalFileProvider,'prepare_attachment',lambda *a:{'name':'other.txt','archive_path':str(file)})
    result=AttachmentContext([event],config).read_attachment('f1')
    assert result['details']['warnings']==['attachment_provider_identity_mismatch']
