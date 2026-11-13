import hashlib
import json

import httpx
import pytest

from messaging import channels


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_TOKEN', 'private-token-never-in-output')
    monkeypatch.setenv('TEST_URL', 'https://workflow.example/secret-path')
    config = {'enabled': True, 'storage': {'assets_dir': str(tmp_path)}}
    target = {'enabled': True, 'kind': 'webhook', 'url_env': 'TEST_URL', 'token_env': 'TEST_TOKEN'}
    envelope = {'delivery_id': 'd1', 'kind': 'upsert', 'task': {
        'task_id': 't1', 'revision': 1, 'title': 'Submit application', 'event': 'Fill and submit the form.',
        'priority': 'P1', 'status': 'open', 'time': {'deadline': '2026-11-15', 'start': None},
        'people': [], 'action_steps': ['Complete the form', 'Send before the deadline'],
        'resources': [], 'sources': [{'ref': 'm1', 'platform': 'qq', 'source': {'database': 'PRIVATE.db'}}]}}
    return target, envelope, config


def test_disabled_channels_never_connect(sample, monkeypatch):
    target, envelope, config = sample
    config['enabled'] = False
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: pytest.fail('no network'))
    with pytest.raises(channels.DeliveryRejected, match='delivery_disabled'):
        channels.deliver(target, envelope, config)


def test_webhook_uses_ack_idempotency_previous_id_and_sanitized_fields(sample, monkeypatch):
    target, envelope, config = sample
    envelope['task']['resources'] = [{'id': 'a1', 'name': 'file.pdf', 'local_path': 'PRIVATE/file.pdf',
        'url': 'file:///PRIVATE/file.pdf', 'type': 'file', 'status': 'ready', 'role': 'input'}]
    def post(url, **kwargs):
        assert kwargs['headers']['Idempotency-Key'] == 'd1'
        assert kwargs['headers']['Authorization'] == 'Bearer private-token-never-in-output'
        assert kwargs['json']['remote_id'] == 'event-1'
        assert 'PRIVATE' not in json.dumps(kwargs['json'])
        assert kwargs['follow_redirects'] is False
        return httpx.Response(200, json={'accepted': True, 'delivery_id': 'd1', 'remote_id': 'event-1'})
    monkeypatch.setattr(httpx, 'post', post)
    result = channels.deliver(target, envelope, config, {'remote_id': 'event-1'})
    assert result['remote_id'] == 'event-1'


@pytest.mark.parametrize('result', [{'ok': True}, {'accepted': True, 'delivery_id': 'wrong'}])
def test_webhook_unconfirmed_ack_is_uncertain(sample, monkeypatch, result):
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: httpx.Response(200, json=result))
    with pytest.raises(channels.DeliveryUncertain, match='acknowledgement'):
        channels.deliver(*sample)


def test_timeout_redacts_credentials(sample, monkeypatch):
    def post(*a, **k):
        raise httpx.ReadTimeout('SECRET URL private-token-never-in-output')
    monkeypatch.setattr(httpx, 'post', post)
    with pytest.raises(channels.DeliveryUncertain) as error:
        channels.deliver(*sample)
    assert str(error.value) == 'delivery_outcome_unknown'


def test_feishu_sign_and_local_material_warning(sample, monkeypatch):
    target, envelope, config = sample
    target.update(kind='feishu', secret_env='TEST_TOKEN')
    monkeypatch.setenv('TEST_URL', 'https://open.feishu.cn/open-apis/bot/v2/hook/secret')
    envelope['task']['resources'] = [{'id': 'a1', 'name': 'form.pdf', 'local_path': 'PRIVATE/a.pdf'}]
    def post(url, **kwargs):
        body = kwargs['json']
        assert body['sign'] and body['timestamp']
        assert 'form.pdf' in body['content']['text']
        assert 'PRIVATE' not in body['content']['text']
        return httpx.Response(200, json={'code': 0})
    monkeypatch.setattr(httpx, 'post', post)
    assert channels.deliver(*sample)['warnings'] == ['local_materials_not_uploaded']


def install_notion_mock(monkeypatch, handler):
    from notion_client import Client
    def factory(token, timeout):
        return Client(auth=token, notion_version='2025-09-03', retry=False,
                      client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(channels, '_notion_client', factory)


def test_notion_paginates_and_updates_existing_without_touching_notes(sample, monkeypatch):
    target, envelope, config = sample
    target.update(kind='notion', data_source_id='database-source')
    envelope['task']['status'] = 'cancelled'
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append((request.method, request.url.path, payload))
        if request.url.path.endswith('/query'):
            if not payload.get('start_cursor'):
                return httpx.Response(200, json={'results': [], 'has_more': True, 'next_cursor': 'next'})
            return httpx.Response(200, json={'results': [{'id': 'existing-page'}], 'has_more': False})
        assert request.method == 'PATCH' and request.url.path.endswith('/pages/existing-page')
        assert set(payload) == {'properties'}
        assert payload['properties']['Status']['select']['name'] == 'cancelled'
        assert payload['properties']['Date']['date']['start'] == '2026-11-15'
        return httpx.Response(200, json={'id': 'existing-page', 'url': 'https://notion.so/existing-page'})
    install_notion_mock(monkeypatch, handler)
    result = channels.deliver(*sample)
    assert result['remote_id'] == 'existing-page' and len(calls) == 3


def test_notion_duplicate_remote_tasks_rejected_without_creating(sample, monkeypatch):
    target, envelope, config = sample
    target.update(kind='notion', data_source_id='source')
    install_notion_mock(monkeypatch, lambda request: httpx.Response(200, json={'results': [{'id': 'a'}, {'id': 'b'}]}))
    with pytest.raises(channels.DeliveryRejected, match='duplicate_task_id'):
        channels.deliver(*sample)


def test_notion_uploads_verified_file_before_attaching_and_reuses_receipt(sample, monkeypatch, tmp_path):
    target, envelope, config = sample
    target.update(kind='notion', data_source_id='source')
    path = tmp_path / 'test.pdf'
    path.write_bytes(b'%PDF-test')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    envelope['task']['resources'] = [{'id': sha, 'name': 'test.pdf', 'local_path': str(path), 'sha256': sha}]
    calls = []
    created = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/query'):
            return httpx.Response(200, json={'results': [{'id': 'page1'}] if created else []})
        if request.url.path == '/v1/file_uploads':
            return httpx.Response(200, json={'id': 'upload1'})
        if request.url.path.endswith('/send'):
            assert b'%PDF-test' in request.content
            return httpx.Response(200, json={'status': 'uploaded'})
        assert request.url.path in {'/v1/pages', '/v1/pages/page1'}
        payload = json.loads(request.content)
        assert payload['properties']['Materials']['files'][0]['file_upload']['id'] == 'upload1'
        assert 'local_path' not in request.content.decode()
        created.append(True)
        return httpx.Response(200, json={'id': 'page1', 'url': 'https://notion.so/page1'})
    install_notion_mock(monkeypatch, handler)
    receipt = channels.deliver(*sample)
    assert receipt['uploads'] == {sha: 'upload1'}
    calls.clear()
    channels.deliver(*sample, previous=receipt)
    assert all('file_uploads' not in path for path in calls)


def test_notion_failed_upload_does_not_claim_delivery(sample, monkeypatch, tmp_path):
    target, envelope, config = sample
    target.update(kind='notion', data_source_id='source')
    path = tmp_path / 'test.pdf'; path.write_bytes(b'file')
    envelope['task']['resources'] = [{'id': 'a', 'name': 'a.pdf', 'local_path': str(path), 'sha256': hashlib.sha256(b'file').hexdigest()}]
    def handler(request):
        assert request.url.path != '/v1/pages'
        if request.url.path.endswith('/query'):
            return httpx.Response(200, json={'results': []})
        if request.url.path.endswith('/send'):
            return httpx.Response(200, json={'status': 'failed'})
        return httpx.Response(200, json={'id': 'upload1'})
    install_notion_mock(monkeypatch, handler)
    with pytest.raises(channels.DeliveryRejected, match='upload_not_complete'):
        channels.deliver(*sample)


def test_notion_rejects_changed_material_before_network(sample, monkeypatch, tmp_path):
    target, envelope, config = sample
    target.update(kind='notion', data_source_id='source')
    path = tmp_path / 'test.pdf'; path.write_bytes(b'wrong')
    envelope['task']['resources'] = [{'id': 'a', 'name': 'a.pdf', 'local_path': str(path), 'sha256': '0' * 64}]
    monkeypatch.setattr(channels, '_notion_client', lambda *a: pytest.fail('must not connect'))
    with pytest.raises(channels.DeliveryRejected, match='integrity_mismatch'):
        channels.deliver(*sample)


def test_notion_text_chunking_preserves_instructions(sample, monkeypatch):
    target, envelope, config = sample
    target.update(kind='notion', data_source_id='source')
    envelope['task']['event'] = 'a' * 5000
    def handler(request):
        if request.url.path.endswith('/query'):
            return httpx.Response(200, json={'results': []})
        parts = json.loads(request.content)['properties']['Instructions']['rich_text']
        assert all(len(p['text']['content']) <= 2000 for p in parts)
        assert ''.join(p['text']['content'] for p in parts).startswith('a' * 5000)
        return httpx.Response(200, json={'id': 'page1'})
    install_notion_mock(monkeypatch, handler)
    channels.deliver(*sample)


def test_outbound_nested_metadata_is_projected_and_result_notes_preserved(sample):
    _, envelope, _ = sample
    task = envelope['task']
    task['time']['private'] = {'path': 'PRIVATE.db'}
    task['people'] = [{'name': 'Alice', 'role': 'owner', 'private': {'path': 'PRIVATE.db'}}]
    task['resources'] = [{'id': 'r1', 'name': 'Done.pdf', 'role': 'result', 'type': 'file',
                          'note': '已提交的签字版', 'private': {'path': 'PRIVATE.db'}}]
    projected = channels.public_envelope(envelope)
    assert 'PRIVATE' not in json.dumps(projected)
    assert projected['task']['resources'][0]['note'] == '已提交的签字版'
    text = channels._instructions(task)
    assert '完成成果：Done.pdf' in text and '成果说明：已提交的签字版' in text
    assert '参与人员：Alice' in text


@pytest.mark.parametrize('field,value', [
    ('action_steps', [{'step': 'legitimate', 'path': 'PRIVATE'}]),
    ('people', [{'name': {'path': 'PRIVATE'}, 'role': 'owner'}]),
    ('time', {'deadline': {'path': 'PRIVATE'}}),
])
def test_nested_objects_cannot_masquerade_as_text(sample, monkeypatch, field, value):
    sample[1]['task'][field] = value
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: pytest.fail('no network'))
    with pytest.raises(channels.DeliveryRejected, match='invalid_delivery_payload'):
        channels.deliver(*sample)


def test_actual_notion_sdk_has_automatic_retry_disabled():
    client = channels._notion_client('test-only', 5)
    try:
        assert client.options.retry is False and client._max_retries == 0
    finally:
        client.close()
