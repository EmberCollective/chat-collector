"""Project provider facts into scoped AI references, without reading platform data."""


def project_identities(groups):
    accounts, account_refs, person_refs, chats, senders = [], {}, {}, {}, {}

    def person_ref(account, identifier):
        key = (*account, identifier)
        if key not in person_refs:
            person_refs[key] = f'u{len(person_refs)+1}'
        return person_refs[key]

    for group in groups:
        account_key = (group['platform'], group['account_id'])
        snapshots = [e['message'].get('identity', {}) for e in group['messages']]
        account = next((s['account'] for s in snapshots if s.get('account')), {})
        if account_key not in account_refs:
            account_refs[account_key] = f'a{len(accounts)+1}'
            accounts.append({'ref':account_refs[account_key], 'platform':group['platform'],
                'label':account.get('label'), 'self':{'ref':person_ref(account_key,group['account_id']),
                                                   'nickname':account.get('nickname')}})
        chat = next((s['conversation'] for s in snapshots if s.get('conversation')), {})
        people = {}
        for event in group['messages']:
            message = event['message']
            identifier = message['sender_id'] or 'unknown:'+event['message_key']
            ref = person_ref(account_key,identifier)
            # Recheck scope even if a provider accidentally supplies its whole address book.
            allowed = {message['sender_id'], *message.get('mentioned_user_ids', [])}
            if message.get('reply_sender_id'):
                allowed.add(message['reply_sender_id'])
            if message.get('conversation_type') == 'direct':
                allowed.add(message['conversation_id'])
            supplied = [p for p in message.get('identity', {}).get('people', []) if p.get('id') in allowed and p.get('id')]
            if not any(p['id'] == message['sender_id'] for p in supplied):
                supplied.append({'id':identifier})
            sender = next((p for p in supplied if p['id']==identifier), {})
            senders[event['message_key']] = {'ref':ref,'label':sender.get('label') or sender.get('display_name') or '名称未获取'}
            for person in supplied:
                p_ref = person_ref(account_key,person['id'])
                row = {k:person.get(k) for k in ('display_name','nickname','remark','group_nickname')}
                row.update(ref=p_ref, is_self=person['id']==group['account_id'],
                           missing_fields=[k for k in ('display_name','nickname','remark','group_nickname') if not person.get(k)])
                previous = people.get(p_ref, {})
                # Prefer available facts over an empty snapshot; do not resolve names here.
                if not previous.get('display_name') or row.get('display_name'):
                    people[p_ref] = row
        chats[group['id']] = {'account_ref':account_refs[account_key], 'type':chat.get('type') or group['messages'][0]['message'].get('conversation_type'),
            'name':chat.get('name'), 'remark':chat.get('remark'), 'people':list(people.values())}
    return accounts, chats, senders
