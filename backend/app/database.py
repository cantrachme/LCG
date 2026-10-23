"""Minimal optional PostgreSQL persistence; no browser state or raw files."""
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

_engine = None
_tables = None


def configured():
    return bool(os.environ.get('DATABASE_URL'))


def storage():
    global _engine, _tables
    if not configured():
        raise HTTPException(503, 'PostgreSQL persistence requires DATABASE_URL.')
    if _engine is None:
        try:
            from sqlalchemy import (create_engine, MetaData, Table, Column, String, Text,
                                    DateTime, ForeignKey, Uuid)
            from sqlalchemy.dialects.postgresql import JSONB
            from sqlalchemy.engine import make_url
            url = make_url(os.environ['DATABASE_URL'])
            if url.get_backend_name() != 'postgresql':
                raise ValueError('PostgreSQL required')
            if url.drivername == 'postgresql':
                url = url.set(drivername='postgresql+psycopg')
            engine = create_engine(url, pool_pre_ping=True, hide_parameters=True,
                                   connect_args={'connect_timeout': 5, 'options': '-c statement_timeout=5000'})
            metadata = MetaData()
            def id_column():
                return Column('id', Uuid(as_uuid=False), primary_key=True)
            def created():
                return Column('created_at', DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
            conversations = Table('conversations', metadata, id_column(), Column('title', String(200), nullable=False), created())
            messages = Table('messages', metadata, id_column(),
                Column('conversation_id', Uuid(as_uuid=False), ForeignKey('conversations.id', ondelete='CASCADE'), nullable=False, index=True),
                Column('role', String(20), nullable=False), Column('content', Text, nullable=False), created())
            documents = Table('documents', metadata, id_column(),
                Column('conversation_id', Uuid(as_uuid=False), ForeignKey('conversations.id', ondelete='SET NULL'), index=True),
                Column('extraction', JSONB, nullable=False), Column('metadata', JSONB, nullable=False), created())
            runs = Table('workflow_runs', metadata, id_column(),
                Column('conversation_id', Uuid(as_uuid=False), ForeignKey('conversations.id', ondelete='SET NULL'), index=True),
                Column('document_id', Uuid(as_uuid=False), ForeignKey('documents.id'), nullable=False, index=True),
                Column('state', String(40), nullable=False), Column('data', JSONB, nullable=False), created(),
                Column('updated_at', DateTime(timezone=True), nullable=False))
            approvals = Table('approvals', metadata, id_column(),
                Column('workflow_run_id', Uuid(as_uuid=False), ForeignKey('workflow_runs.id'), nullable=False, index=True),
                Column('summary', Text, nullable=False), Column('decision', String(20), nullable=False), created())
            _tables = {t.name: t for t in (conversations, messages, documents, runs, approvals)}
            _engine = engine
        except Exception:
            raise HTTPException(503, 'PostgreSQL configuration or dependencies are unavailable. Install SQLAlchemy and psycopg and check DATABASE_URL.') from None
    return _engine, _tables


@contextmanager
def transaction():
    engine, tables = storage()
    try:
        with engine.begin() as connection:
            yield connection, tables
    except HTTPException:
        raise
    except Exception:
        # SQL/driver exceptions can contain URLs, credentials and document values.
        raise HTTPException(503, 'Database operation failed; no successful persistence is confirmed.') from None


def initialize():
    with transaction() as (connection, tables):
        tables['conversations'].metadata.create_all(connection)
        # Never resume automation merely because the backend restarted.
        runs = tables['workflow_runs']
        connection.execute(runs.update().where(runs.c.state.in_(['inspecting', 'running', 'queued'])).values(
            state='paused', updated_at=datetime.now(timezone.utc)))


def dispose():
    global _engine, _tables
    if _engine is not None:
        _engine.dispose()
    _engine = _tables = None


def row(connection, table, identifier):
    found = connection.execute(table.select().where(table.c.id == str(identifier))).mappings().first()
    if found is None:
        raise HTTPException(404, 'Record not found.')
    return dict(found)


def save_document(document, filename, conversation_id=None):
    with transaction() as (connection, tables):
        if conversation_id:
            row(connection, tables['conversations'], conversation_id)
        connection.execute(tables['documents'].insert().values(
            id=document.extraction_id, conversation_id=str(conversation_id) if conversation_id else None,
            extraction=document.model_dump(mode='json'),
            metadata={'filename': filename, 'page_count': document.page_count, 'format': 'pdf', 'schema_version': 1}))


def latest_document():
    with transaction() as (connection, tables):
        table = tables['documents']
        return connection.execute(table.select().order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().first()


def save_workflow(job):
    with transaction() as (connection, tables):
        from sqlalchemy.dialects.postgresql import insert
        document = row(connection, tables['documents'], job['extraction_id'])
        # Explicit allowlist: never serialize the browser/session object or config.
        data = {key: job[key] for key in ('request_id', 'stage', 'version', 'events', 'result') if key in job}
        values = dict(id=job['id'], document_id=job['extraction_id'], conversation_id=document['conversation_id'],
                      state=job['state'], data=data, updated_at=datetime.now(timezone.utc))
        statement = insert(tables['workflow_runs']).values(**values)
        connection.execute(statement.on_conflict_do_update(index_elements=['id'], set_={
            key: value for key, value in values.items() if key != 'id'}))


def latest_workflow(document_id):
    with transaction() as (connection, tables):
        table = tables['workflow_runs']
        found = connection.execute(table.select().where(table.c.document_id == document_id)
            .order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().first()
        if not found:
            return None
        return {'id': str(found['id']), 'extraction_id': str(found['document_id']), **found['data'], 'state': found['state']}


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ConversationInput(Payload):
    title: str = Field(min_length=1, max_length=200)


class MessageInput(Payload):
    role: Literal['user', 'assistant', 'system']
    content: str = Field(min_length=1, max_length=20000)


class WorkflowInput(Payload):
    document_id: UUID


class WorkflowUpdate(Payload):
    state: Literal['queued', 'running', 'paused', 'completed', 'failed', 'cancelled']
    stage: str = Field(default='', max_length=500)


class ApprovalInput(Payload):
    workflow_run_id: UUID
    summary: str = Field(min_length=1, max_length=2000)


class ApprovalUpdate(Payload):
    decision: Literal['approved', 'rejected']


def persistence_router(local_guard):
    def no_cache(response: Response):
        response.headers['Cache-Control'] = 'no-store'
    router = APIRouter(prefix='/api/persistence', dependencies=[Depends(local_guard), Depends(no_cache)])

    @router.post('/conversations', status_code=201)
    def create_conversation(body: ConversationInput):
        with transaction() as (c, t):
            identifier = str(uuid4())
            c.execute(t['conversations'].insert().values(id=identifier, title=body.title))
            return row(c, t['conversations'], identifier)

    @router.get('/conversations')
    def conversations(limit: int = 50, offset: int = 0):
        return listing('conversations', limit, offset)

    @router.get('/conversations/{identifier}')
    def conversation(identifier: UUID):
        return read('conversations', identifier)

    @router.patch('/conversations/{identifier}')
    def update_conversation(identifier: UUID, body: ConversationInput):
        return update('conversations', identifier, {'title': body.title})

    @router.delete('/conversations/{identifier}', status_code=204)
    def delete_conversation(identifier: UUID):
        with transaction() as (c, t):
            row(c, t['conversations'], identifier)
            c.execute(t['conversations'].delete().where(t['conversations'].c.id == str(identifier)))

    @router.post('/conversations/{identifier}/messages', status_code=201)
    def add_message(identifier: UUID, body: MessageInput):
        with transaction() as (c, t):
            row(c, t['conversations'], identifier)
            message_id = str(uuid4())
            c.execute(t['messages'].insert().values(id=message_id, conversation_id=str(identifier), **body.model_dump()))
            return row(c, t['messages'], message_id)

    @router.get('/conversations/{identifier}/messages')
    def messages(identifier: UUID, limit: int = 50, offset: int = 0):
        return listing('messages', limit, offset, 'conversation_id', identifier)

    @router.get('/conversations/{identifier}/documents')
    def conversation_documents(identifier: UUID, limit: int = 50, offset: int = 0):
        return listing('documents', limit, offset, 'conversation_id', identifier)

    @router.get('/documents')
    def documents(limit: int = 50, offset: int = 0):
        return listing('documents', limit, offset)

    @router.get('/documents/{identifier}')
    def document(identifier: UUID):
        return read('documents', identifier)

    @router.post('/workflow-runs', status_code=201)
    def create_workflow(body: WorkflowInput):
        identifier = str(uuid4())
        save_workflow({'id': identifier, 'extraction_id': str(body.document_id), 'state': 'queued',
                       'request_id': str(uuid4()), 'stage': '', 'version': 0, 'events': [], 'result': None})
        return read('workflow_runs', identifier)

    @router.get('/workflow-runs')
    def workflows(limit: int = 50, offset: int = 0):
        return listing('workflow_runs', limit, offset)

    @router.get('/workflow-runs/{identifier}')
    def workflow(identifier: UUID):
        return read('workflow_runs', identifier)

    @router.patch('/workflow-runs/{identifier}')
    def update_workflow(identifier: UUID, body: WorkflowUpdate):
        # Persistence only: this endpoint never starts/resumes browser automation.
        with transaction() as (c, t):
            existing = row(c, t['workflow_runs'], identifier)
            data = {**existing['data'], 'stage': body.stage}
            c.execute(t['workflow_runs'].update().where(t['workflow_runs'].c.id == str(identifier)).values(
                state=body.state, data=data, updated_at=datetime.now(timezone.utc)))
            return row(c, t['workflow_runs'], identifier)

    @router.post('/approvals', status_code=201)
    def create_approval(body: ApprovalInput):
        with transaction() as (c, t):
            row(c, t['workflow_runs'], body.workflow_run_id)
            identifier = str(uuid4())
            c.execute(t['approvals'].insert().values(id=identifier, workflow_run_id=str(body.workflow_run_id), summary=body.summary, decision='pending'))
            return row(c, t['approvals'], identifier)

    @router.get('/approvals/{identifier}')
    def approval(identifier: UUID):
        return read('approvals', identifier)

    @router.patch('/approvals/{identifier}')
    def decide(identifier: UUID, body: ApprovalUpdate):
        return update('approvals', identifier, {'decision': body.decision})

    return router


def read(name, identifier):
    with transaction() as (c, t):
        return row(c, t[name], identifier)


def update(name, identifier, values):
    with transaction() as (c, t):
        row(c, t[name], identifier)
        c.execute(t[name].update().where(t[name].c.id == str(identifier)).values(**values))
        return row(c, t[name], identifier)


def listing(name, limit, offset, foreign_key=None, identifier=None):
    if not 1 <= limit <= 100 or offset < 0:
        raise HTTPException(422, 'Use limit 1–100 and a nonnegative offset.')
    with transaction() as (c, t):
        table = t[name]
        query = table.select()
        if foreign_key:
            row(c, t['conversations'], identifier)
            query = query.where(table.c[foreign_key] == str(identifier))
        return [dict(r) for r in c.execute(query.order_by(table.c.created_at, table.c.id).limit(limit).offset(offset)).mappings()]
