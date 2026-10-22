# LCG Command Center frontend

React, TypeScript and plain CSS Command Center with PDF contract extraction.

With the existing dependencies installed, run `npm run dev` in this directory
and open `http://localhost:5173` (or `http://127.0.0.1:5173`). The existing
FastAPI backend must run separately at `http://127.0.0.1:8000`.
Use port 5173 because the backend currently permits those two frontend origins.

The header checks `GET http://127.0.0.1:8000/api/health` on page load and when
**Check connection** / **Retry connection** is selected. A successful response
must include `status: "ok"` and `service: "lcg-backend"`. Requests time out after
eight seconds. Status reflects the most recent check; it does not poll.

The AI Coworker panel and Browser Agent panel stack on tablet and mobile screens.
Select a PDF in AI Coworker and choose **Extract contract**. The frontend sends a
multipart `file` to `POST /api/contracts/extract`, then presents original source
values, normalized values, missing fields, and review warnings. Date values use
ISO format; monetary values use decimal strings to preserve precision. The source
contract is returned under `original`; future user-approved changes must be kept
separate. No editing or LCW submission is implemented.

The backend uses PyMuPDF text extraction and deterministic labeled-line parsing
for the supplied training-contract format. Limits: 5 MB, 10 pages, and 100,000
extracted characters. Scanned pages, encrypted/unreadable files, and unsupported
formats are rejected. Missing fields are never populated from sample constants.
PRA reuse instructions and training-only notes are preserved. The LCW-generated
contract number remains unassigned when the source says it will be assigned later.
The frontend request times out after 30 seconds.

Uploaded files and extracted values are not retained by the application or logged.
FastAPI may temporarily spool multipart uploads; each upload is closed after the
request. Review data exists in browser memory until replacement or page reload.
The browser workspace remains informational; chat and automation are not connected.

Run backend checks from `backend/` with `../.venv/bin/python -m pytest -q`.
Tests generate synthetic PDFs in memory. Start the existing backend with
`../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
from that directory. No dependency installation is needed when the manually
prepared environment is available.

Checks: `npm run build` runs TypeScript project checks and the Vite production build.
`npm run lint` runs the existing linter. `npm run preview` serves the built frontend;
its default port is not included in the backend's development CORS allowlist.
