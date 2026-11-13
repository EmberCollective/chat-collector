import hashlib
import importlib
import json
from pathlib import Path

import pymupdf
import pytest
import tomlkit

from providers.attachments import AttachmentContext


@pytest.fixture(params=['qq','wechat'])
def setup(request, tmp_path, monkeypatch):
    platform = request.param
    media = tmp_path/'media'; media.mkdir()
    path = media/'notice.pdf'
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72,72),'Automatic attachment resolution')
        pdf.save(path)
    collector = {'platform':platform,'source':{'account_id':'fixture','media_dir':str(media),
        'decrypted_dir':str(tmp_path/'plain'),'refresh':False},
        'storage':{'state_file':str(tmp_path/'collector.db'),'output_dir':str(tmp_path/'batches')},
        'processing':{'intensity':'metadata'},'attachments':{'enabled':True,'archive_dir':str(tmp_path/'archive')}}
    config_path = tmp_path/'collector.toml'
    config_path.write_bytes(tomlkit.dumps(collector).encode('utf-8'))
    attachment = {'type':'file','name':'notice.pdf','status':'deferred'}
    message = {'platform':platform,'account_id':'fixture','conversation_id':'room','conversation_type':'group',
        'message_id':'1','sender_id':'person','sent_at':100,'text':'See notice','kind':'file','source':{},'attachments':[attachment]}
    fresh = dict(message, attachments=[dict(attachment,local_path=str(path))])
    adapter = importlib.import_module('collector.adapters.'+platform)
    scans=[]
    def scan(source,start,end,**kwargs):
        scans.append((start,end,kwargs))
        return iter([fresh])
    monkeypatch.setattr(adapter,'scan',scan)
    config = {'storage':{'state_file':str(tmp_path/'processor.db')},'model':{'vision':True},
        'attachments':{'archive_roots':[str(tmp_path/'archive')],'cache_dir':str(tmp_path/'cache'),
                       'collector_configs':{platform:str(config_path)}}}
    return config, {'message_key':'key','message':message}, path, config_path, scans


def test_metadata_is_resolved_by_ref_on_demand_and_archive_is_reused(setup, monkeypatch):
    config,event,path,config_path,scans=setup
    original=path.read_bytes(); settings=config_path.read_bytes()
    reader=AttachmentContext([event],config)
    assert reader.catalog()[0]['status']=='resolvable'
    assert not scans and not Path(config['attachments']['archive_roots'][0]).exists()
    reply=reader.read_attachment('f1')
    assert reply['details']['status']=='ok'
    assert 'Automatic attachment resolution' in json.dumps(reply)
    assert len(scans)==1 and scans[0][:2]==(100,101)
    resolved=reader.resolved[('key',0)]
    assert Path(resolved['archive_path']).read_bytes()==original
    assert path.read_bytes()==original and config_path.read_bytes()==settings
    assert not (config_path.parent/'collector.db').exists()
    def forbidden(*args,**kwargs): raise AssertionError('Existing archives must not be resolved again')
    monkeypatch.setattr(reader.resolver,'resolve',forbidden)
    assert reader.read_attachment('f1')['details']['status']=='ok'
    # A new Pi session can reuse an on-demand archive even when old JSONL has only metadata.
    fresh_reader=AttachmentContext([event],config)
    adapter=importlib.import_module('collector.adapters.'+event['message']['platform'])
    monkeypatch.setattr(adapter,'scan',forbidden)
    assert fresh_reader.read_attachment('f1')['details']['status']=='ok'
    event['message']['attachments']=[resolved]
    reused=AttachmentContext([event],config)
    monkeypatch.setattr(reused.resolver,'resolve',forbidden)
    assert reused.read_attachment('f1')['details']['status']=='ok'
    assert str(path) not in json.dumps(reply)


def test_account_mismatch_fails_without_scanning_other_account(setup):
    config,event,path,config_path,scans=setup
    event['message']['account_id']='different-account'
    reader=AttachmentContext([event],config)
    result=reader.read_attachment('f1')
    assert result['details']['warnings']==['attachment_account_mismatch']
    assert not scans


def test_cached_attachment_identity_is_checked_before_reuse(setup):
    config, event, path, config_path, scans = setup
    assert AttachmentContext([event], config).read_attachment('f1')['details']['status'] == 'ok'
    cached = next((Path(config['attachments']['cache_dir'])/'resolved').glob('*.json'))
    saved = json.loads(cached.read_text(encoding='utf-8'))
    saved['name'] = 'different-attachment.pdf'
    cached.write_text(json.dumps(saved), encoding='utf-8')
    assert AttachmentContext([event], config).read_attachment('f1')['details']['status'] == 'ok'
    assert len(scans) == 2
    assert json.loads(cached.read_text(encoding='utf-8'))['name'] == 'notice.pdf'


def test_audio_capability_is_reported_without_false_missing_file_error(setup, monkeypatch):
    config, event, path, config_path, scans = setup
    event['message']['attachments'] = [{'type': 'voice'}]
    reader = AttachmentContext([event], config)
    def forbidden(*args):
        raise AssertionError('Unsupported audio must not attempt document resolution')
    monkeypatch.setattr(reader.resolver, 'resolve', forbidden)
    assert reader.catalog()[0]['status'] == 'needs_review'
    result = reader.read_attachment('f1')['details']
    assert result['warnings'] == ['audio_video_understanding_unavailable']
    assert 'transcription' in result['next_action']
    assert result['evidence'] == []


def test_missing_local_file_reports_action_without_inventing_content(setup):
    config,event,path,config_path,scans=setup
    path.unlink()
    reader=AttachmentContext([event],config)
    result=reader.read_attachment('f1')
    assert result['details']['status']=='needs_review'
    assert result['details']['evidence']==[] and result['details']['next_action']
    assert str(path) not in json.dumps(result)


def test_text_and_unsupported_binary_are_not_misclassified_as_images(tmp_path):
    archive=tmp_path/'archive';archive.mkdir()
    text=archive/'notice.md';text.write_text('Please submit tomorrow',encoding='utf-8')
    binary=archive/'installer.exe';binary.write_bytes(b'MZ executable bytes')
    config={'storage':{'state_file':str(tmp_path/'state.db')},'attachments':{'archive_roots':[str(archive)]}}
    events=[{'message_key':'key','message':{'attachments':[{'name':p.name,'archive_path':str(p)} for p in [text,binary]]}}]
    reader=AttachmentContext(events,config)
    assert reader.catalog()[0]['type']=='text'
    assert 'Please submit tomorrow' in json.dumps(reader.read_attachment('f1'))
    assert reader.read_attachment('f2')['details']['warnings']==['unsupported_attachment_format']


def test_on_demand_read_waits_for_active_collector(setup):
    import threading
    from collector.__main__ import InstanceLock
    config,event,path,config_path,scans=setup
    lock=InstanceLock(config_path.parent/'collector.db')
    lock.__enter__()
    release=threading.Timer(.15,lock.__exit__)
    release.start()
    try:
        assert AttachmentContext([event],config).read_attachment('f1')['details']['status']=='ok'
    finally:
        release.join()


def test_collector_lock_wait_has_a_deadline(tmp_path):
    from collector.__main__ import InstanceLock
    with InstanceLock(tmp_path/'state'):
        with pytest.raises(RuntimeError,match='Another collector'):
            with InstanceLock(tmp_path/'state',timeout=.02):
                pytest.fail('A concurrent lock must not succeed')
