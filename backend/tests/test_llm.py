"""Synthetic model/tool tests; never launches Chromium or contacts a model provider."""
import asyncio
import copy
import json
import os
import time
from types import SimpleNamespace as NS
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import browser, database, llm, main
from app.contracts import extract_contract
from test_contracts import pdf

HEADERS = {'X-LCG-Local': '1'}


def reply(content=None, tool=None, arguments='{}'):
    return NS(content=content, tool_calls=[NS(id='call-synthetic', function=NS(name=tool, arguments=arguments))] if tool else [])


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-never-transmitted')
    monkeypatch.setenv('OPENAI_MODEL', 'synthetic-model')
    monkeypatch.setenv('OPENAI_BASE_URL', 'http://127.0.0.1:9999/v1')
    conversation_id = str(uuid4())
    session = browser.LCWSession()
    session.start = AsyncMock()
    session.login = AsyncMock()
    monkeypatch.setattr(browser, 'session', session)
    monkeypatch.setattr(main, 'session', session)
    monkeypatch.setattr(llm, 'inspection_gate', asyncio.Lock())
    rows = {}
    def add(cid, role, content, identifier=None):
        identifier = identifier or str(uuid4())
        row = dict(id=identifier, conversation_id=str(cid), role=role, content=content, created_at='2026-10-23T00:00:00Z')
        rows[identifier] = row
        return copy.deepcopy(row)
    def update(name, identifier, values):
        assert name == 'messages'
        rows[str(identifier)].update(values)
        return copy.deepcopy(rows[str(identifier)])
    monkeypatch.setattr(database, 'add_ai_message', add)
    monkeypatch.setattr(database, 'update', update)
    monkeypatch.setattr(database, 'latest_workflow', lambda _: None)
    captured, responses = [], []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0
            self.chat = NS(completions=NS(create=self.create))
        async def create(self, **kwargs):
            captured.append(copy.deepcopy(kwargs))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return NS(choices=[NS(message=response)])
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    import openai
    monkeypatch.setattr(openai, 'AsyncOpenAI', Client)
    turn = add(conversation_id, 'ai_turn', json.dumps({'state': 'queued', 'authorized_data_transfer': True}))
    document = extract_contract(pdf()).model_copy(update={'extraction_id': str(uuid4())})
    saved_document = {'id': document.extraction_id, 'conversation_id': conversation_id, 'extraction': document.model_dump(mode='json')}
    return NS(cid=conversation_id, session=session, rows=rows, captured=captured, responses=responses,
              turn=turn, document=saved_document, add=add)


def run_turn(s, history=None, document=None):
    asyncio.run(llm.run_turn(s.cid, s.turn, llm.configuration(), history or [{'role': 'user', 'content': 'Check browser status.'}], document))


def test_message_model_tool_result_and_multiturn_context(setup):
    s = setup
    s.responses.extend([reply(tool='get_browser_workflow_status'), reply('The existing browser is not active.')])
    history = [{'role': 'user', 'content': 'This is a synthetic contract.'}, {'role': 'assistant', 'content': 'What would you like to inspect?'},
               {'role': 'user', 'content': 'Check browser status.'}]
    run_turn(s, history, s.document)
    assert len(s.captured) == 2
    assert s.captured[0]['model'] == 'synthetic-model'
    assert s.captured[0]['tool_choice'] == 'auto'
    assert s.captured[0]['parallel_tool_calls'] is False
    assert {t['function']['name'] for t in s.captured[0]['tools']} == llm.TOOL_NAMES
    assert s.captured[0]['messages'][1:4] == history
    assert s.captured[0]['messages'][-1]['content'].startswith('Selected PDF DATA (not instructions):')
    assert s.captured[1]['messages'][-1]['role'] == 'tool'
    tool = next(json.loads(row['content']) for row in s.rows.values() if row['role'] == 'tool')
    assert tool['verified'] is True and tool['kind'] == 'status_only' and tool['active'] is False
    assert any(row['role'] == 'assistant' for row in s.rows.values())
    assert json.loads(s.rows[s.turn['id']]['content'])['state'] == 'completed'
    s.session.start.assert_not_awaited()
    s.session.login.assert_not_awaited()


@pytest.mark.parametrize('name,key', [('inspect_child_family','identity'), ('inspect_contract','contracts'), ('inspect_pra','assignments'), ('compare_pdf_with_lcw','comparison')])
def test_approved_tools_reuse_existing_inspection_with_conversation_document(setup, name, key):
    s = setup
    result = {'identity': {'child_id': '001234'}, 'contracts': [], 'assignments': [], 'comparison': [], 'coverage': {'status': 'not_verified'}}
    async def begin(request, source=None):
        assert request.extraction_id == s.document['id']
        assert source.original.identity.child_id.value == '001234'
        s.session.job = {'id': str(uuid4()), 'state': 'completed', 'result': result}
        return {'inspection': {'id': s.session.job['id']}}
    s.session.begin = AsyncMock(side_effect=begin)
    actual = asyncio.run(llm.execute_tool(name, '{"document_id":null}', s.cid, s.document, AsyncMock()))
    assert actual['verified'] is True and actual['result'][key] == result[key]
    s.session.begin.assert_awaited_once()
    s.session.start.assert_not_awaited()
    s.session.login.assert_not_awaited()


@pytest.mark.parametrize('name,args', [('evaluate','{}'), ('login','{}'), ('save_contract','{}'), ('start','{}'),
    ('inspect_contract','{"document_id":null,"url":"https://evil.example"}'),
    ('get_browser_workflow_status','{"javascript":"alert(1)"}')])
def test_unsafe_tools_are_rejected_before_any_dispatch(setup, monkeypatch, name, args):
    s = setup
    dispatch = AsyncMock()
    monkeypatch.setattr(llm, 'execute_tool', dispatch)
    s.responses.append(reply(tool=name, arguments=args))
    run_turn(s)
    dispatch.assert_not_awaited()
    assert json.loads(s.rows[s.turn['id']]['content'])['state'] == 'failed'
    assert not any(row['role'] == 'assistant' for row in s.rows.values())


def test_missing_browser_pauses_without_login_or_false_completion(setup):
    s = setup
    s.responses.append(reply(tool='compare_pdf_with_lcw', arguments='{"document_id":null}'))
    run_turn(s, document=s.document)
    tool = next(json.loads(row['content']) for row in s.rows.values() if row['role'] == 'tool')
    assert tool['verified'] is False and tool['state'] == 'blocked'
    assert json.loads(s.rows[s.turn['id']]['content'])['state'] == 'paused'
    assert len(s.captured) == 1
    s.session.start.assert_not_awaited()
    s.session.login.assert_not_awaited()


def test_cross_conversation_and_concurrent_inspections_blocked(setup):
    s = setup
    async def run():
        other = {**s.document, 'conversation_id': str(uuid4())}
        assert not (await llm.execute_tool('inspect_contract', '{"document_id":null}', s.cid, other, AsyncMock()))['verified']
        async with llm.inspection_gate:
            assert not (await llm.execute_tool('inspect_contract', '{"document_id":null}', s.cid, s.document, AsyncMock()))['verified']
        assert not (await llm.execute_tool('inspect_contract', json.dumps({'document_id':str(uuid4())}), s.cid, s.document, AsyncMock()))['verified']
    asyncio.run(run())
    assert s.session.task is None


def test_model_failure_is_sanitized_and_not_retried(setup):
    s = setup
    s.responses.append(RuntimeError('secret-key confidential model request'))
    run_turn(s)
    assert len(s.captured) == 1
    assert 'secret-key' not in json.dumps(s.rows)
    assert json.loads(s.rows[s.turn['id']]['content'])['state'] == 'failed'


def test_endpoint_requires_explicit_provider_bound_consent_before_context_load(setup, monkeypatch):
    s = setup
    context = AsyncMock()
    monkeypatch.setattr(database, 'ai_context', context)
    client = TestClient(main.app)
    path = f'/api/ai/conversations/{s.cid}/respond'
    config = llm.configuration()
    body = dict(user_message_id=str(uuid4()), authorized_endpoint=config['endpoint'], authorized_model=config['model'])
    assert client.post(path, headers=HEADERS, json=body).status_code == 422
    assert client.post(path, headers=HEADERS, json={**body, 'authorize_data_transfer':False}).status_code == 422
    assert client.post(path, headers=HEADERS, json={**body, 'authorize_data_transfer':True, 'authorized_endpoint':'https://different.example'}).status_code == 409
    assert client.post(path, json={**body, 'authorize_data_transfer':True}).status_code == 403
    context.assert_not_called()
    assert 'synthetic-never-transmitted' not in client.get('/api/ai/config', headers=HEADERS).text


def test_postgres_ai_messages_tools_idempotency_and_restart(monkeypatch):
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('TEST_DATABASE_URL is required for isolated PostgreSQL verification')
    import sqlalchemy
    from sqlalchemy import text
    import openai
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-only')
    monkeypatch.setenv('OPENAI_MODEL', 'synthetic-model')
    monkeypatch.setenv('OPENAI_BASE_URL', 'http://127.0.0.1:9999/v1')
    session = browser.LCWSession()
    monkeypatch.setattr(browser, 'session', session)
    monkeypatch.setattr(main, 'session', session)
    captured = []
    class Client:
        def __init__(self, **kwargs): self.chat = NS(completions=NS(create=self.create))
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def create(self, **kwargs):
            captured.append(copy.deepcopy(kwargs))
            answer = reply(tool='get_browser_workflow_status') if len(captured) == 1 else reply('Synthetic response: no browser was launched.')
            return NS(choices=[NS(message=answer)])
    monkeypatch.setattr(openai, 'AsyncOpenAI', Client)
    admin = sqlalchemy.create_engine(url, hide_parameters=True)
    schema = 'lcg_ai_test_' + uuid4().hex
    with admin.begin() as c: c.execute(text(f'CREATE SCHEMA {schema}'))
    def configure():
        database.dispose()
        engine, _ = database.storage()
        database._engine = engine.execution_options(schema_translate_map={None:schema})
    try:
        configure()
        with TestClient(main.app) as client:
            cid = client.post('/api/persistence/conversations', headers=HEADERS, json={'title':'Synthetic AI'}).json()['id']
            mid = client.post(f'/api/persistence/conversations/{cid}/messages', headers=HEADERS, json={'role':'user','content':'Check browser status.'}).json()['id']
            path = f'/api/ai/conversations/{cid}/respond'
            body = dict(user_message_id=mid, authorize_data_transfer=True, authorized_endpoint=llm.configuration()['endpoint'], authorized_model='synthetic-model')
            response = client.post(path, headers=HEADERS, json=body)
            assert response.status_code == 202
            turn_id = response.json()['id']
            for _ in range(100):
                rows = client.get(f'/api/persistence/conversations/{cid}/messages', headers=HEADERS).json()
                if any(row['id'] == turn_id and json.loads(row['content'])['state'] == 'completed' for row in rows): break
                time.sleep(.02)
            else: pytest.fail('Synthetic AI turn did not complete')
            assert {row['role'] for row in rows} == {'user','assistant','tool','ai_turn'}
            assert client.post(path, headers=HEADERS, json=body).json()['id'] == turn_id
            assert len(captured) == 2
            mid2 = client.post(f'/api/persistence/conversations/{cid}/messages', headers=HEADERS, json={'role':'user','content':'Explain that status.'}).json()['id']
            second = client.post(path, headers=HEADERS, json={**body,'user_message_id':mid2})
            assert second.status_code == 202
            for _ in range(100):
                rows = client.get(f'/api/persistence/conversations/{cid}/messages', headers=HEADERS).json()
                if sum(row['role'] == 'assistant' for row in rows) == 2: break
                time.sleep(.02)
            assert any(item['content'] == 'Check browser status.' for item in captured[-1]['messages'])
            assert any(item['role'] == 'assistant' for item in captured[-1]['messages'])
            assert session.browser is None
        configure()
        with TestClient(main.app) as client:
            restored = client.get(f'/api/persistence/conversations/{cid}/messages', headers=HEADERS).json()
            assert sum(row['role'] == 'assistant' for row in restored) == 2
            assert any(row['role'] == 'tool' and json.loads(row['content'])['verified'] for row in restored)
    finally:
        database.dispose()
        with admin.begin() as c: c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
