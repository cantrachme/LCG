
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from . import database
from .contracts import MAX_FILE_BYTES, ContractExtraction, ExtractionError, extract_contract
from .browser import local_request, router as browser_router, viewer_router, session


@asynccontextmanager
async def lifespan(app: FastAPI):
    if database.configured():
        await run_in_threadpool(database.initialize)
        saved = await run_in_threadpool(database.latest_document)
        if saved:
            session.set_document(ContractExtraction.model_validate(saved['extraction']))
            job = await run_in_threadpool(database.latest_workflow, session.document_id)
            if job:
                session.job = {**job, 'source': session.document.model_copy(deep=True)}
                session.owner = 'human'
                session.resume.clear()
    try:
        yield
    finally:
        await session.shutdown()
        database.dispose()

app = FastAPI(
    title="LCG Command Center API",
    description="Backend for the LCG AI Browser Agent",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(browser_router)
app.include_router(viewer_router)
app.include_router(database.persistence_router(local_request))

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
    expose_headers=["X-LCG-Captured-At", "X-LCG-Persistence"],
)


@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "service": "lcg-backend",
    }


@app.post("/api/contracts/extract", response_model=ContractExtraction, dependencies=[Depends(local_request)])
async def extract_uploaded_contract(response: Response, file: UploadFile = File(...), conversation_id: UUID | None = None):
    response.headers["Cache-Control"] = "no-store"
    try:
        if not (file.filename or "").lower().endswith(".pdf") or file.content_type not in {"application/pdf", "application/octet-stream"}:
            raise HTTPException(status_code=415, detail="Upload a PDF file (.pdf). Other file types are not supported.")
        if file.size is not None and file.size > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="The PDF exceeds the 5 MB size limit.")
        content = await file.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="The PDF exceeds the 5 MB size limit.")
        if not content:
            raise HTTPException(status_code=400, detail="The uploaded PDF is empty. Choose a non-empty PDF.")
        try:
            document = await run_in_threadpool(extract_contract, content)
            document = document.model_copy(update={'extraction_id': str(uuid4())})
            if database.configured():
                await run_in_threadpool(database.save_document, document, file.filename, conversation_id)
                response.headers['X-LCG-Persistence'] = 'postgresql'
            else:
                if conversation_id:
                    raise HTTPException(503, 'Conversation persistence requires DATABASE_URL.')
                response.headers['X-LCG-Persistence'] = 'disabled'
            return session.set_document(document)
        except ExtractionError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
    finally:
        await file.close()


@app.post('/api/contracts/{identifier}/select', response_model=ContractExtraction, dependencies=[Depends(local_request)])
async def select_saved_contract(identifier: UUID, response: Response):
    """Select saved extraction for the existing inspection UI; never operates Chromium."""
    response.headers['Cache-Control'] = 'no-store'
    saved = await run_in_threadpool(database.read, 'documents', identifier)
    if session.task and not session.task.done() and session.document_id != str(identifier):
        raise HTTPException(409, 'Stop the current inspection before selecting another document.')
    return session.set_document(ContractExtraction.model_validate(saved['extraction']))
