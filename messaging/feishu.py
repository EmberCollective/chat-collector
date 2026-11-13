"""Feishu app bot: official SDK transport, durable cards and authenticated callbacks."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import html
import importlib.metadata
import json
from pathlib import Path
import secrets
import time

from .channels import DeliveryRejected, DeliveryUncertain, _secret, _url, public_envelope


SCHEMA = '''
CREATE TABLE IF NOT EXISTS feishu_cards(
 delivery_id TEXT PRIMARY KEY, scope TEXT, target_id TEXT, task_id TEXT, revision INTEGER,
 generation TEXT, message_id TEXT, created_at REAL, status TEXT, card TEXT);
CREATE TABLE IF NOT EXISTS feishu_actions(token TEXT PRIMARY KEY, delivery_id TEXT, action TEXT);
CREATE TABLE IF NOT EXISTS feishu_heads(scope TEXT,task_id TEXT,generation TEXT,PRIMARY KEY(scope,task_id));
CREATE TABLE IF NOT EXISTS feishu_tests(delivery_id TEXT PRIMARY KEY,created_at REAL,clicked_at REAL,action TEXT);
CREATE TABLE IF NOT EXISTS feishu_files(
 scope TEXT,task_id TEXT,asset_id TEXT,file_key TEXT,message_id TEXT,status TEXT,error TEXT,
 PRIMARY KEY(scope,task_id,asset_id));
'''


def _state(config):
    from .feedback import _connect
    path = Path(config.get('feedback', {}).get('state_file') or Path(config['storage']['state_file']).with_name('feedback.db'))
    settings = dict(config, feedback=dict(config.get('feedback', {}), state_file=str(path)))
    conn = _connect(settings, write=True)
    conn.executescript(SCHEMA)
    return conn


def _scope(target, config):
    identity = [target['id'], _secret(target, 'app_id_env', config),
                target.get('receive_id_type', 'chat_id'), target.get('recipient_id')]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def check(config, target):
    missing = []
    for field in ('app_id_env', 'app_secret_env'):
        if not _secret(target, field, config, required=False):
            missing.append(field)
    if not target.get('recipient_id'):
        missing.append('recipient_id')
    if target.get('receive_id_type', 'chat_id') != 'chat_id':
        missing.append('receive_id_type_must_be_chat_id')
    operators = target.get('allowed_operator_ids')
    if not isinstance(operators, list) or not operators or any(not isinstance(x, str) or not x.startswith('ou_') for x in operators):
        missing.append('allowed_operator_ids')
    try:
        version = importlib.metadata.version('lark-oapi')
    except importlib.metadata.PackageNotFoundError:
        version = None
        missing.append('lark_oapi_sdk')
    return {'configured': not missing, 'missing': missing, 'sdk_version': version,
            'enabled': bool(config.get('enabled') and target.get('enabled')), 'network_checked': False}


def _client(target, config):
    import lark_oapi as lark
    return (lark.Client.builder().app_id(_secret(target, 'app_id_env', config))
            .app_secret(_secret(target, 'app_secret_env', config))
            .timeout(target.get('timeout_seconds', 30)).log_level(lark.LogLevel.ERROR).build())


def _call(function, request):
    try:
        response = function(request)
    except Exception:
        raise DeliveryUncertain('feishu_app_request_outcome_unknown') from None
    if not response.success():
        raise DeliveryRejected('feishu_app_request_rejected_' + str(response.code))
    return response.data


def _plain(text):
    # Untrusted chat text cannot introduce mentions, card HTML, or fake action links.
    return ''.join(f'&#{ord(c)};' if c in '*~<>[]()#_`' else html.escape(c) for c in str(text))


def card_content(task, tokens, *, alert=False, upload_files=True):
    done = task.get('status') in {'completed', 'done'}
    color = 'red' if alert or task['priority'] == 'P0' else 'green' if done else 'blue'
    steps = task.get('action_steps') or [task['event']]
    main = '\n'.join(f'{i}. {_plain(step)}' for i, step in enumerate(steps, 1))
    details = [f"**状态** {_plain(task.get('status', 'open'))}"]
    deadline = task.get('time', {}).get('deadline')
    if deadline:
        details.append('**截止** ' + _plain(deadline))
    if task.get('location'):
        details.append('**地点** ' + _plain(task['location']))
    if task.get('people'):
        details.append('**参与人员** ' + '；'.join(_plain(f"{p['name']}（{p['role']}）") for p in task['people']))
    elements = [{'tag': 'markdown', 'content': ('**故障说明**\n' if alert else '**你需要做**\n') + main},
        {'tag': 'column_set', 'flex_mode': 'none', 'background_style': 'grey-50', 'columns': [
            {'tag': 'column', 'width': 'weighted', 'weight': 1, 'elements': [
                {'tag': 'markdown', 'content': '\n'.join(details)}]}]}]
    materials = []
    for r in task.get('resources', []):
        label = '成果' if r.get('role') == 'result' else '材料'
        if _url(r.get('url')):
            line = f"{label}：[{_plain(r['name'])}]({r['url'].replace(')', '%29').replace('(', '%28')})"
        else:
            state = ('文件另发于本群；失败详情见本地投递记录' if upload_files else '仅本地归档，未开启文件发送') if r.get('local_path') else '原件尚不可用'
            line = f"{label}：{_plain(r['name'])} — {state}"
        if r.get('note'):
            line += '；' + _plain(r['note'])
        materials.append(line)
    if materials:
        elements.append({'tag': 'markdown', 'content': '**相关资料**\n' + '\n'.join(materials)})
    notes = []
    explanations = task.get('notes') or task.get('review_reasons', [])
    if explanations:
        notes.append('说明：' + '；'.join(explanations))
    for source in task.get('sources', []):
        notes.append('来源：' + ' · '.join(str(source[k]) for k in ('platform', 'conversation', 'sender', 'time') if source.get(k)))
    if notes:
        elements.append({'tag': 'markdown', 'content': _plain('\n'.join(notes)), 'text_size': 'notation'})
    if tokens and not alert:
        labels = {'complete': '完成待办', 'reopen': '恢复待办', 'snooze': '稍后提醒'}
        buttons = [{'tag': 'column', 'width': 'weighted', 'weight': 1, 'elements': [{
            'tag': 'button', 'type': 'primary_filled' if i == 0 else 'default', 'width': 'fill',
            'text': {'tag': 'plain_text', 'content': labels[action]},
            'behaviors': [{'type': 'callback', 'value': {'token': token}}]}]}
            for i, (action, token) in enumerate(tokens.items())]
        elements.append({'tag': 'column_set', 'flex_mode': 'none', 'columns': buttons})
    card = {'schema': '2.0', 'config': {'update_multi': True, 'width_mode': 'default', 'enable_forward': False},
            'header': {'title': {'tag': 'plain_text', 'content': f"[{task['priority']}] {task['title']}"},
                       'template': color, 'icon': {'tag': 'standard_icon', 'token': 'todo_colorful'}},
            'body': {'direction': 'vertical', 'vertical_spacing': '12px', 'elements': elements}}
    encoded = json.dumps(card, ensure_ascii=False)
    if len(encoded.encode()) > 28000:
        raise DeliveryRejected('feishu_card_too_large')
    return card


def _send(client, target, kind, content, identifier):
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    body = (CreateMessageRequestBody.builder().receive_id(target['recipient_id']).msg_type(kind)
            .content(json.dumps(content, ensure_ascii=False)).uuid(hashlib.sha256(identifier.encode()).hexdigest()[:50]).build())
    request = CreateMessageRequest.builder().receive_id_type('chat_id').request_body(body).build()
    data = _call(client.im.v1.message.create, request)
    if not data or not getattr(data, 'message_id', None):
        raise DeliveryUncertain('feishu_message_receipt_missing')
    return data.message_id


def _file_delivery(client, target, task, resource, config, scope):
    from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
    from .package import material_path
    key = (scope, task['task_id'], resource['id'])
    with closing(_state(config)) as conn:
        row = conn.execute('SELECT * FROM feishu_files WHERE scope=? AND task_id=? AND asset_id=?', key).fetchone()
        if row and row['status'] in {'sent', 'unknown', 'sending'}:
            return {'id': resource['id'], 'status': 'sent' if row['status'] == 'sent' else 'unknown',
                    'message_id': row['message_id']}
    try:
        path, name = material_path(task, resource['id'], config)
        limit = min(int(target.get('max_file_bytes', 30_000_000)), 30_000_000)
        if not 0 < path.stat().st_size <= limit:
            raise DeliveryRejected('feishu_file_size_unsupported')
        file_key = row['file_key'] if row else None
        if not file_key:
            with path.open('rb') as stream:
                body = CreateFileRequestBody.builder().file_type('stream').file_name(name).file(stream).build()
                data = _call(client.im.v1.file.create, CreateFileRequest.builder().request_body(body).build())
            file_key = getattr(data, 'file_key', None)
            if not file_key:
                raise DeliveryUncertain('feishu_file_receipt_missing')
        with closing(_state(config)) as conn, conn:
            conn.execute('INSERT INTO feishu_files VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,task_id,asset_id) DO UPDATE SET file_key=excluded.file_key,status=excluded.status,error=NULL',
                         (*key, file_key, None, 'sending', None))
        message_id = _send(client, target, 'file', {'file_key': file_key}, 'file:' + ':'.join(key))
        with closing(_state(config)) as conn, conn:
            conn.execute("UPDATE feishu_files SET message_id=?,status='sent',error=NULL WHERE scope=? AND task_id=? AND asset_id=?", (message_id, *key))
        return {'id': resource['id'], 'status': 'sent', 'message_id': message_id}
    except Exception as exc:
        uncertain = not isinstance(exc, (DeliveryRejected, ValueError, OSError))
        state = 'unknown' if uncertain else 'failed'
        error = str(exc) if isinstance(exc, (DeliveryRejected, DeliveryUncertain)) else 'feishu_material_unavailable'
        with closing(_state(config)) as conn, conn:
            conn.execute('INSERT INTO feishu_files(scope,task_id,asset_id,status,error) VALUES(?,?,?,?,?) ON CONFLICT(scope,task_id,asset_id) DO UPDATE SET status=excluded.status,error=excluded.error', (*key, state, error))
        return {'id': resource['id'], 'status': state, 'error': error}


def deliver_app(target, envelope, config, previous=None):
    public_envelope(envelope)
    if not config.get('enabled') or not target.get('enabled'):
        raise DeliveryRejected('delivery_disabled')
    validation = check(config, target)
    if not validation['configured']:
        raise DeliveryRejected('feishu_app_not_configured')
    task, identifier = envelope['task'], envelope['delivery_id']
    scope = _scope(target, config)
    alert = envelope['kind'] == 'alert'
    with closing(_state(config)) as conn, conn:
        row = conn.execute('SELECT * FROM feishu_cards WHERE delivery_id=?', (identifier,)).fetchone()
        if row and (row['scope'] != scope or row['task_id'] != task['task_id'] or row['revision'] != task['revision']):
            raise DeliveryRejected('feishu_delivery_identity_conflict')
        if row and row['status'] in {'sending', 'unknown'}:
            raise DeliveryUncertain('feishu_card_prior_outcome_unknown')
        if not row:
            actions = [] if alert or task['status'] in {'cancelled', 'superseded'} else ['reopen'] if task['status'] in {'completed', 'done'} else ['complete', 'snooze']
            tokens = {a: secrets.token_urlsafe(24) for a in actions}
            card = card_content(task, tokens, alert=alert, upload_files=target.get('upload_files', True))
            conn.execute('INSERT INTO feishu_cards VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (identifier, scope, target['id'], task['task_id'], task['revision'], secrets.token_hex(16), None, time.time(), 'prepared', json.dumps(card)))
            conn.executemany('INSERT INTO feishu_actions VALUES(?,?,?)', [(token, identifier, action) for action, token in tokens.items()])
            row = conn.execute('SELECT * FROM feishu_cards WHERE delivery_id=?', (identifier,)).fetchone()
        head = conn.execute('SELECT c.* FROM feishu_heads h JOIN feishu_cards c ON c.scope=h.scope AND c.task_id=h.task_id AND c.generation=h.generation WHERE h.scope=? AND h.task_id=?', (scope, task['task_id'])).fetchone()
    client = _client(target, config)
    if row['status'] not in {'sent', 'acknowledged'}:
        with closing(_state(config)) as conn, conn:
            conn.execute("UPDATE feishu_cards SET status='sending' WHERE delivery_id=?", (identifier,))
        try:
            if not alert and envelope['kind'] == 'upsert' and head and head['message_id'] and time.time() - head['created_at'] < 13 * 86400:
                from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
                request = (PatchMessageRequest.builder().message_id(head['message_id'])
                           .request_body(PatchMessageRequestBody.builder().content(row['card']).build()).build())
                _call(client.im.v1.message.patch, request)
                message_id = head['message_id']
                created_at = head['created_at']
            else:
                message_id = _send(client, target, 'interactive', json.loads(row['card']), identifier)
                created_at = time.time()
            with closing(_state(config)) as conn, conn:
                conn.execute("UPDATE feishu_cards SET message_id=?,created_at=?,status='sent' WHERE delivery_id=?", (message_id, created_at, identifier))
                if not alert:
                    conn.execute('INSERT INTO feishu_heads VALUES(?,?,?) ON CONFLICT(scope,task_id) DO UPDATE SET generation=excluded.generation', (scope, task['task_id'], row['generation']))
        except Exception as exc:
            with closing(_state(config)) as conn, conn:
                conn.execute('UPDATE feishu_cards SET status=? WHERE delivery_id=?', ('prepared' if isinstance(exc, DeliveryRejected) else 'unknown', identifier))
            raise
    else:
        message_id = row['message_id']
    files = []
    if not alert and target.get('upload_files', True):
        for resource in task.get('resources', []):
            if resource.get('local_path'):
                files.append(_file_delivery(client, target, task, resource, config, scope))
    receipt = {'delivery_id': identifier, 'remote_id': message_id, 'message_id': message_id, 'files': files}
    if row['status'] == 'acknowledged':
        receipt.update(manual_acknowledged=True, callbacks_unavailable=True)
    if any(f['status'] == 'unknown' for f in files):
        raise DeliveryUncertain('feishu_material_delivery_unknown', receipt=receipt)
    if any(f['status'] != 'sent' for f in files):
        raise DeliveryRejected('feishu_material_delivery_failed', receipt=receipt)
    return receipt


def reconcile_delivery(target, envelope, config, action):
    """Only called after an explicit human retry/delivered decision in the core."""
    if action not in {'retry', 'delivered'}:
        raise ValueError('invalid_feishu_reconciliation')
    scope = _scope(target, config)
    task, identifier = envelope['task'], envelope['delivery_id']
    with closing(_state(config)) as conn, conn:
        card = conn.execute('SELECT * FROM feishu_cards WHERE delivery_id=?', (identifier,)).fetchone()
        if card and (card['scope'] != scope or card['target_id'] != target['id'] or card['task_id'] != task['task_id']):
            raise ValueError('feishu_reconciliation_scope_mismatch')
        if action == 'retry':
            conn.execute("UPDATE feishu_cards SET status='prepared' WHERE delivery_id=? AND status IN ('unknown','sending')", (identifier,))
        elif card and card['status'] != 'sent':
            # Acknowledgement cannot invent a message ID or authenticate an unknown card's buttons.
            conn.execute("UPDATE feishu_cards SET status='acknowledged' WHERE delivery_id=?", (identifier,))
            conn.execute('DELETE FROM feishu_actions WHERE delivery_id=?', (identifier,))
            conn.execute('DELETE FROM feishu_heads WHERE scope=? AND task_id=? AND generation=?',
                         (scope, task['task_id'], card['generation']))
        files = []
        for resource in task.get('resources', []):
            key = (scope, task['task_id'], resource['id'])
            stored = conn.execute('SELECT * FROM feishu_files WHERE scope=? AND task_id=? AND asset_id=?', key).fetchone()
            if not stored:
                continue
            if stored['status'] != 'sent':
                conn.execute('UPDATE feishu_files SET status=?,error=NULL WHERE scope=? AND task_id=? AND asset_id=?',
                             ('failed' if action == 'retry' else 'sent', *key))
            files.append({'id': resource['id'], 'status': 'sent' if action == 'delivered' else stored['status'],
                          'message_id': stored['message_id']})
        if action == 'retry':
            return None
        receipt = {'delivery_id': identifier, 'manual_acknowledged': True, 'files': files}
        if card and card['status'] == 'sent' and card['message_id']:
            receipt.update(message_id=card['message_id'], remote_id=card['message_id'])
        else:
            receipt['callbacks_unavailable'] = True
        return receipt


def handle_callback(config, target, event, *, enqueue=None):
    """Only the SDK's authenticated WS dispatcher should call this entry point."""
    if not config.get('enabled') or not target.get('enabled'):
        raise ValueError('feishu_actions_disabled')
    try:
        operator = event['event']['operator']['open_id']
        context = event['event']['context']
        value = event['event']['action']['value']
        header = event['header']
    except (KeyError, TypeError):
        raise ValueError('feishu_callback_invalid') from None
    if (operator not in target.get('allowed_operator_ids', [])
            or header.get('app_id') != _secret(target, 'app_id_env', config)
            or header.get('event_type') != 'card.action.trigger'
            or event['event']['action'].get('tag') != 'button'
            or not isinstance(value, dict) or set(value) != {'token'} or not isinstance(value['token'], str)):
        raise ValueError('feishu_callback_not_authorized')
    with closing(_state(config)) as conn:
        row = conn.execute('SELECT a.action,c.*,h.generation AS head_generation FROM feishu_actions a JOIN feishu_cards c ON c.delivery_id=a.delivery_id LEFT JOIN feishu_heads h ON h.scope=c.scope AND h.task_id=c.task_id WHERE a.token=?', (value['token'],)).fetchone()
    if (not row or row['status'] != 'sent' or row['scope'] != _scope(target, config)
            or row['target_id'] != target['id'] or row['generation'] != row['head_generation']
            or row['message_id'] != context.get('open_message_id')
            or context.get('open_chat_id') != target.get('recipient_id')):
        raise ValueError('feishu_card_expired_or_unrecognized')
    with closing(_state(config)) as conn, conn:
        tested = conn.execute('UPDATE feishu_tests SET clicked_at=COALESCE(clicked_at,?),action=COALESCE(action,?) WHERE delivery_id=?',
                              (time.time(), row['action'], row['delivery_id'])).rowcount
    if tested:
        return {'status': 'test_received', 'test_id': row['delivery_id'], 'synthetic': True}
    if enqueue is None:
        from .feedback import enqueue_feedback
        enqueue = enqueue_feedback
    return enqueue(config, target['id'], 'feishu:' + hashlib.sha256(value['token'].encode()).hexdigest(),
                   row['task_id'], row['action'], {'platform': 'feishu', 'user_id': operator, 'source': 'card'},
                   expected_revision=row['revision'])


def dispatch_callback(config, listener_app_id, event):
    """Route only stored tokens for this connected app to the current target policy."""
    try:
        value = event['event']['action']['value']
        token = value['token']
    except (KeyError, TypeError):
        raise ValueError('feishu_callback_invalid') from None
    if not isinstance(token, str) or not isinstance(value, dict) or set(value) != {'token'}:
        raise ValueError('feishu_callback_invalid')
    with closing(_state(config)) as conn:
        row = conn.execute('SELECT c.target_id FROM feishu_actions a JOIN feishu_cards c ON c.delivery_id=a.delivery_id WHERE a.token=?', (token,)).fetchone()
    selected = next((t for t in config.get('targets', []) if row and t['id'] == row['target_id']), None)
    if (not selected or selected.get('kind') != 'feishu_app'
            or _secret(selected, 'app_id_env', config, required=False) != listener_app_id):
        raise ValueError('feishu_callback_not_authorized')
    return handle_callback(config, selected, event)


def listen(config, target, *, setup=False, stop_file=None):
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    if not setup and (not check(config, target)['configured'] or not config.get('enabled') or not target.get('enabled')):
        raise ValueError('feishu_listener_not_enabled_or_configured')
    listener_app_id = _secret(target, 'app_id_env', config)
    def callback(data):
        if setup:
            return P2CardActionTriggerResponse({'toast': {'type': 'info', 'content': '连接检查中，尚未启用待办操作。'}})
        try:
            from .config import load_config
            current = load_config(config['_config_path']) if config.get('_config_path') else config
            result = dispatch_callback(current, listener_app_id, json.loads(lark.JSON.marshal(data)))
            message = '操作已接收，正在更新待办。' if result.get('status') != 'rejected' else '操作未接受，请刷新待办后重试。'
            if result.get('status') == 'test_received':
                message = '测试回调成功，未修改正式待办。'
            return P2CardActionTriggerResponse({'toast': {'type': 'info', 'content': message}})
        except Exception:
            return P2CardActionTriggerResponse({'toast': {'type': 'error', 'content': '此卡片已过期、未获授权或服务暂不可用，请使用最新卡片。'}})
    handler = lark.EventDispatcherHandler.builder('', '').register_p2_card_action_trigger(callback).build()
    ws = lark.ws.Client(listener_app_id, _secret(target, 'app_secret_env', config),
                        event_handler=handler, log_level=lark.LogLevel.CRITICAL, auto_reconnect=True)
    from lark_oapi.ws.client import loop
    async def observe():
        connected = False
        while True:
            if ws._conn is not None and not connected:
                print(json.dumps({'status': 'connected', 'mode': 'setup' if setup else 'listen', 'target': target['id']}), flush=True)
                connected = True
            if ws._conn is None:
                connected = False
            if stop_file and Path(stop_file).exists():
                # shortcut: pinned SDK exposes no public stop; replace when its lifecycle API supports it.
                ws._auto_reconnect = False
                await ws._disconnect()
                for task in asyncio.all_tasks(loop):
                    if task is not asyncio.current_task():
                        task.cancel()
                return
            await asyncio.sleep(.5)
    observer = loop.create_task(observe())
    try:
        ws.start()
    except asyncio.CancelledError:
        if not stop_file or not Path(stop_file).exists():
            raise
    finally:
        observer.cancel()
        ws._auto_reconnect = False
        loop.run_until_complete(ws._disconnect())
        remaining = list(asyncio.all_tasks(loop))
        for task in remaining:
            task.cancel()
        if remaining:
            loop.run_until_complete(asyncio.gather(*remaining, return_exceptions=True))


def list_chats(config, target, *, chat_id=None):
    from lark_oapi.api.im.v1 import ListChatRequest, GetChatMembersRequest
    client = _client(target, config)
    items, token, seen = [], None, set()
    while True:
        if chat_id:
            builder = GetChatMembersRequest.builder().chat_id(chat_id).member_id_type('open_id').page_size(100)
            call = client.im.v1.chat_members.get
        else:
            builder = ListChatRequest.builder().user_id_type('open_id').page_size(100)
            call = client.im.v1.chat.list
        if token:
            builder.page_token(token)
        data = _call(call, builder.build())
        for item in data.items or []:
            items.append({'name': item.name, 'open_id' if chat_id else 'chat_id': item.member_id if chat_id else item.chat_id})
        if not data.has_more:
            return {'items': items, 'complete': not bool(getattr(data, 'trigger_security_conf_limit', False))}
        token = data.page_token
        if not token or token in seen:
            raise ValueError('feishu_pagination_invalid')
        seen.add(token)


def main(argv=None):
    from .config import load_config
    parser = argparse.ArgumentParser(description='Official Feishu app-bot SDK setup and callback listener.')
    parser.add_argument('mode', choices=['check', 'list-chats', 'list-members', 'listen'])
    parser.add_argument('--config', default='config/messaging.toml')
    parser.add_argument('--target', default='feishu-assistant')
    parser.add_argument('--chat-id')
    parser.add_argument('--setup', action='store_true', help='Connect only, never apply callbacks.')
    parser.add_argument('--stop-file', help='Exit when this local supervisor signal file exists.')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        target = next(t for t in config['targets'] if t['id'] == args.target and t['kind'] == 'feishu_app')
        if args.mode == 'check':
            result = check(config, target)
        elif args.mode == 'listen':
            listen(config, target, setup=args.setup, stop_file=args.stop_file)
            return 0
        else:
            chat = (args.chat_id or target.get('recipient_id')) if args.mode == 'list-members' else None
            if args.mode == 'list-members' and not chat:
                raise ValueError('recipient_id_required_for_members')
            result = list_chats(config, target, chat_id=chat)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        error = str(exc) if isinstance(exc, (DeliveryRejected, DeliveryUncertain)) else 'feishu_command_failed'
        print(json.dumps({'status': 'failed', 'error': error}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
