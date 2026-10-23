
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from .contracts import MAX_FILE_BYTES, ContractExtraction, ExtractionError, extract_contract
from .browser import local_request, router as browser_router, session


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await session.shutdown()

app = FastAPI(
    title="LCG Command Center API",
    description="Backend for the LCG AI Browser Agent",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(browser_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=["X-LCG-Captured-At"],
)


@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "service": "lcg-backend",
    }


@app.post("/api/contracts/extract", response_model=ContractExtraction, dependencies=[Depends(local_request)])
async def extract_uploaded_contract(response: Response, file: UploadFile = File(...)):
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
            return session.set_document(await run_in_threadpool(extract_contract, content))
        except ExtractionError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
    finally:
        await file.close()
