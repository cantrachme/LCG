"""Offline boundary tests plus opt-in PostgreSQL CRUD/restart verification. No browser launch."""
import os
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import database, main, browser
from test_contracts import pdf

HEADERS = {'X-LCG-Local': '1'}


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    database.dispose()
    monkeypatch.delenv('DATABASE_URL', raising=False)
    session = browser.LCWSession()
    monkeypatch.setattr(main, 'session', session)
    monkeypatch.setattr(browser, 'session', session)
    yield
    database.dispose()


def test_unconfigured_database_is_explicit_and_existing_extraction_still_works():
    with TestClient(main.app) as client:
        assert client.get('/api/health').status_code == 200
        assert client.get('/api/persistence/documents', headers=HEADERS).status_code == 503
        result = client.post('/api/contracts/extract', headers=HEADERS, files={'file': ('synthetic.pdf', pdf(), 'application/pdf')})
        assert result.status_code == 200
        assert result.headers['x-lcg-persistence'] == 'disabled'
        assert main.session.document_id == result.json()['extraction_id']
        assert client.post('/api/contracts/extract', params={'conversation_id': str(uuid4())}, headers=HEADERS,
                           files={'file': ('synthetic.pdf', pdf(), 'application/pdf')}).status_code == 503


def test_database_routes_require_local_access_and_validate_payloads():
    client = TestClient(main.app)
    assert client.post('/api/persistence/conversations', json={'title': 'test'}).status_code == 403
    assert client.post('/api/persistence/conversations', headers=HEADERS, json={'title': 'test', 'password': 'forbidden'}).status_code == 422
    assert client.post('/api/persistence/conversations', headers={**HEADERS, 'Origin': 'https://evil.example'}, json={'title': 'test'}).status_code == 403
    assert client.get('/api/persistence/documents/not-a-uuid', headers=HEADERS).status_code == 422
    assert client.get('/api/persistence/documents?limit=500', headers=HEADERS).status_code == 422
    assert client.patch('/api/persistence/approvals/'+str(uuid4()), headers=HEADERS, json={'decision': 'execute'}).status_code == 422


def test_configuration_failure_does_not_expose_url(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'sqlite://secret-password')
    response = TestClient(main.app).get('/api/persistence/documents', headers=HEADERS)
    assert response.status_code == 503
    assert 'secret-password' not in response.text


def test_pdf_saved_before_session_update_and_restored_without_browser(monkeypatch):
    stored = []
    monkeypatch.setattr(database, 'configured', lambda: True)
    monkeypatch.setattr(database, 'initialize', lambda: None)
    monkeypatch.setattr(database, 'latest_document', lambda: stored[-1] if stored else None)
    monkeypatch.setattr(database, 'latest_workflow', lambda _: None)
    def save(document, filename, conversation_id):
        assert main.session.document is None
        stored.append({'extraction': document.model_dump(mode='json'), 'metadata': {'filename': filename}})
    monkeypatch.setattr(database, 'save_document', save)
    with TestClient(main.app) as client:
        response = client.post('/api/contracts/extract', headers=HEADERS, files={'file': ('synthetic.pdf', pdf(), 'application/pdf')})
        assert response.status_code == 200
        assert response.headers['x-lcg-persistence'] == 'postgresql'
        data = response.json()
        assert stored[0]['extraction'] == data
    replacement = browser.LCWSession()
    replacement.start = AsyncMock()
    monkeypatch.setattr(main, 'session', replacement)
    monkeypatch.setattr(browser, 'session', replacement)
    with TestClient(main.app) as client:
        assert client.get('/api/browser/document', headers=HEADERS).json() == data
        replacement.start.assert_not_awaited()


def test_failed_persistence_does_not_claim_saved_extraction(monkeypatch):
    monkeypatch.setattr(database, 'configured', lambda: True)
    def fail(*args):
        raise HTTPException(503, 'Database unavailable.')
    monkeypatch.setattr(database, 'save_document', fail)
    response = TestClient(main.app).post('/api/contracts/extract', headers=HEADERS, files={'file': ('synthetic.pdf', pdf(), 'application/pdf')})
    assert response.status_code == 503
    assert main.session.document is None


def test_workflow_failure_stops_further_actions_and_retains_source(monkeypatch):
    monkeypatch.setattr(database, 'configured', lambda: True)
    def fail(*args):
        raise HTTPException(503, 'Database unavailable.')
    monkeypatch.setattr(database, 'save_workflow', fail)
    session = main.session
    session.job = {'id': str(uuid4()), 'state': 'inspecting', 'version': 0, 'events': []}
    session.progress('Reading')
    assert session.owner == 'human'
    assert session.job['state'] == 'failed'
    assert not session.resume.is_set()


def test_postgres_schema_crud_extraction_and_restart(monkeypatch):
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('TEST_DATABASE_URL is not configured; real PostgreSQL verification unavailable')
    sqlalchemy = pytest.importorskip('sqlalchemy')
    pytest.importorskip('psycopg')
    from sqlalchemy import text, inspect
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    assert parsed.get_backend_name() == 'postgresql'
    if parsed.drivername == 'postgresql':
        parsed = parsed.set(drivername='postgresql+psycopg')
    admin = sqlalchemy.create_engine(parsed, hide_parameters=True)
    schema = 'lcg_test_' + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {schema}'))
    monkeypatch.setenv('DATABASE_URL', parsed.render_as_string(hide_password=False))
    def configure_schema():
        engine, _ = database.storage()
        database._engine = engine.execution_options(schema_translate_map={None: schema})
    try:
        configure_schema()
        with TestClient(main.app) as client:
            with admin.connect() as connection:
                assert set(inspect(connection).get_table_names(schema=schema)) == {'conversations', 'messages', 'documents', 'workflow_runs', 'approvals'}
            created = client.post('/api/persistence/conversations', headers=HEADERS, json={'title': 'Synthetic'})
            assert created.status_code == 201
            conversation = created.json()['id']
            assert client.patch('/api/persistence/conversations/'+conversation, headers=HEADERS, json={'title': 'Updated'}).json()['title'] == 'Updated'
            message = client.post(f'/api/persistence/conversations/{conversation}/messages', headers=HEADERS, json={'role': 'user', 'content': 'Synthetic message'})
            assert message.status_code == 201
            assert len(client.get(f'/api/persistence/conversations/{conversation}/messages', headers=HEADERS).json()) == 1
            result = client.post('/api/contracts/extract', params={'conversation_id': conversation}, headers=HEADERS, files={'file': ('synthetic.pdf', pdf(), 'application/pdf')})
            assert result.status_code == 200
            extracted = result.json()
            document_id = extracted['extraction_id']
            saved = client.get('/api/persistence/documents/'+document_id, headers=HEADERS).json()
            assert saved['extraction'] == extracted
            assert saved['extraction']['warnings']
            assert saved['extraction']['original']['identity']['child_id']['original'] == '001234'
            assert saved['metadata']['page_count'] == 1
            workflow = client.post('/api/persistence/workflow-runs', headers=HEADERS, json={'document_id': document_id})
            assert workflow.status_code == 201
            run_id = workflow.json()['id']
            assert client.patch('/api/persistence/workflow-runs/'+run_id, headers=HEADERS, json={'state': 'running', 'stage': 'Synthetic'}).status_code == 200
            approval = client.post('/api/persistence/approvals', headers=HEADERS, json={'workflow_run_id': run_id, 'summary': 'Synthetic approval'})
            assert approval.status_code == 201
            approval_id = approval.json()['id']
            assert client.patch('/api/persistence/approvals/'+approval_id, headers=HEADERS, json={'decision': 'approved'}).json()['decision'] == 'approved'
            assert client.get('/api/persistence/documents/'+str(uuid4()), headers=HEADERS).status_code == 404
        # Fresh pool, fresh application session, startup restoration from PostgreSQL.
        replacement = browser.LCWSession()
        monkeypatch.setattr(main, 'session', replacement)
        monkeypatch.setattr(browser, 'session', replacement)
        configure_schema()
        with TestClient(main.app) as client:
            assert client.get('/api/browser/document', headers=HEADERS).json() == extracted
            assert client.get('/api/persistence/workflow-runs/'+run_id, headers=HEADERS).json()['state'] == 'paused'
            assert replacement.job['state'] == 'paused'
            assert replacement.task is None and replacement.browser is None
            assert client.get('/api/persistence/approvals/'+approval_id, headers=HEADERS).json()['decision'] == 'approved'
            assert client.delete('/api/persistence/conversations/'+conversation, headers=HEADERS).status_code == 204
            assert client.get('/api/persistence/conversations/'+conversation, headers=HEADERS).status_code == 404
            assert client.get('/api/persistence/documents/'+document_id, headers=HEADERS).status_code == 200
    finally:
        database.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
