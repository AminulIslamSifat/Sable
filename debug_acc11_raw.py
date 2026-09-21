"""Raw diagnostic: acc11 token -> session -> hello -> chained msg. Dump EVERYTHING."""
import asyncio, json, sys
sys.path.insert(0, '.')

from connectors.deepseek.client import DeepSeekClient, BASE_URL


def load_token() -> tuple[str, str]:
    store = json.loads(open('system/.deepseek_tokens.json').read())
    acct, tok = 'browser-data-acc11', store['browser-data-acc11'][-1]
    return acct, tok


async def main() -> None:
    acct, token = load_token()
    print(f'ACCOUNT: {acct}')
    print(f'TOKEN (first 30): {token[:30]}')

    c = DeepSeekClient(account=acct)
    c._token = token
    c._rotate_tokens = [token]
    c._rotate_idx = 0
    c._account = acct

    http = await c._get_http()
    headers = c._auth_headers_for(token)

    # 1. Session create — raw
    resp = await http.post(f'{BASE_URL}/api/v0/chat_session/create', json={}, headers=headers)
    print(f'\n=== SESSION CREATE: HTTP {resp.status_code} ===')
    body = resp.text
    print(body[:500] if body.strip() else '<<< EMPTY BODY >>>')
    try:
        data = resp.json()
        inner = (data.get('data') or {}).get('biz_data') or {}
        cs = inner.get('chat_session') or {}
        sid = cs.get('id') or inner.get('id')
    except Exception as e:
        print('json parse fail:', e)
        return
    if not sid:
        print('NO SESSION ID — token dead/muted. biz_msg:', data.get('msg'))
        return
    print(f'SESSION: {sid}')

    # 2. Hello — raw SSE dump
    async def send(prompt: str, parent, model_type: str | None, tag: str) -> int | None:
        payload = {
            'chat_session_id': sid,
            'parent_message_id': parent,
            'model_type': model_type,
            'prompt': prompt,
            'ref_file_ids': [],
            'thinking_enabled': False,
            'search_enabled': True,
            'action': None,
            'preempt': False,
        }
        print(f'\n=== {tag}: parent={parent}, model_type={model_type}, prompt_len={len(prompt)} ===')
        last_id = None
        raw_lines = []
        async with http.stream('POST', f'{BASE_URL}/api/v0/chat/completion', json=payload, headers=headers) as r:
            print(f'HTTP {r.status_code}')
            if r.status_code != 200:
                content = await r.aread()
                print('BODY:', content[:500])
                return None
            count = 0
            async for line in r.aiter_lines():
                count += 1
                if count <= 12:
                    raw_lines.append(line[:200])
                if line.startswith('data: ') and line[6:] != '[DONE]':
                    try:
                        obj = json.loads(line[6:])
                        rid = obj.get('response_message_id')
                        if isinstance(rid, int):
                            last_id = rid
                    except Exception:
                        pass
            print(f'total SSE lines: {count}')
        for ln in raw_lines:
            print('  |', ln)
        print(f'{tag} message_id: {last_id}')
        return last_id

    hello_id = await send('hello', None, 'default', 'HELLO')
    if hello_id is None:
        print('\n>>> HELLO GOT NO message_id — dumping first 800 raw chars above shows why')
        return

    # 3. Real message chained to hello
    real_id = await send('Say exactly: chained-ok. Nothing else.', hello_id, None, 'CHAINED-MSG')
    print(f'\n=== RESULT ===')
    print(f'hello_id={hello_id}, chained_id={real_id}')
    print('PASS: full chain works' if real_id else 'FAIL: chained message empty')


asyncio.run(main())
