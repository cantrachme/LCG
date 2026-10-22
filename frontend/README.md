# LCG Command Center frontend

React, TypeScript and plain CSS foundation for the Command Center.

With the existing dependencies installed, run `npm run dev` in this directory
and open `http://localhost:5173` (or `http://127.0.0.1:5173`). The existing
FastAPI backend must run separately at `http://127.0.0.1:8000`.
Use port 5173 because the backend currently permits those two frontend origins.

The header checks `GET http://127.0.0.1:8000/api/health` on page load and when
**Check connection** / **Retry connection** is selected. A successful response
must include `status: "ok"` and `service: "lcg-backend"`. Requests time out after
eight seconds. Status reflects the most recent check; it does not poll.

The AI Coworker panel and Browser Agent panel stack on tablet and mobile screens.
Chat, document uploads, workflow execution, and browser sessions are not connected.
The browser workspace is informational only. No portal access or automation occurs.

Checks: `npm run build` runs TypeScript project checks and the Vite production build.
`npm run lint` runs the existing linter. `npm run preview` serves the built frontend;
its default port is not included in the backend's development CORS allowlist.
