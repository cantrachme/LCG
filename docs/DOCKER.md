# Run LCG with Docker

Use Docker Desktop (Apple Silicon and Intel builds select the native architecture), with Compose 2.30+.

One-time setup:

1. Copy `.env.example` to `.env` if it does not exist. Replace the database password with a random **hex** value (`openssl rand -hex 32`). Keep this file private. This installation already generated the root `.env` when absent.
2. Retain your existing `backend/.env`, or copy `backend/.env.example` there and configure LCW plus your OpenAI-compatible AI endpoint, model and key. Compose passes these settings at runtime, without baking them into images. Gemini uses its OpenAI-compatible endpoint. Configuration presence does not prove the key is valid.
3. Stop the old host backend using port 8000 before starting Compose. Never run multiple app replicas or workers.

Then run:

```sh
docker compose build
docker compose up
```

Open http://localhost:8000. Only loopback port 8000 is published; PostgreSQL has no published port. The app waits for PostgreSQL's health check and initializes tables without resetting them. API/WS calls use the page origin. Vite development retains an `/api` proxy.

`postgres_data` retains chats, messages, extracted document data, workflows and approvals. Raw PDFs are not stored, matching existing behavior. `browser_profile` retains the **Linux** Chromium profile. The existing macOS profile and host PostgreSQL records are untouched and are not automatically migrated into these new volumes. Existing host data requires a deliberate database export/import if needed. Never use `docker compose down -v` for routine shutdown. Use `docker compose stop` or `docker compose down` (without `-v`). Keep the original database password for an initialized volume.

Chromium starts only through the explicit browser-panel action. The separate Linux profile requires manual sign-in/MFA when needed; there is no automatic login or authentication migration. Take Control and Return Control use the existing ownership mechanism. One app worker, init/reaping and 1 GB shared memory support the headless viewer. Container shutdown flushes Chromium before exiting. Stale coordination files are removed only within `/data/browser`, under its exclusive owner lock, after checking the container process table for a profile owner; cookies and profile data are never removed. Containers must not share this profile across PID namespaces outside this Compose setup.

The app runs as a non-root user. Chromium uses container isolation with `--no-sandbox` inside Docker; do not expose the service beyond the configured loopback publication. Docker NAT peers must provide an allowed Origin (or same-origin Fetch Metadata for requests without Origin) in addition to the existing exact Host, HTTP header or single-use WebSocket ticket checks. No wildcard origins or forwarded-header trust are enabled.

## Verified locally

Native ARM64 Docker build and Compose startup succeeded. Both services were healthy; only `127.0.0.1:8000` was published. React, SPA routing, health, conversation/message persistence, PDF extraction/association and a real synthetic AI response were verified. Chat/document records survived database restart and app recreation. Chromium's Linux profile survived recreation. A synthetic page in the single container-controlled Chromium received screencast frames, clicks and typing over the application's port-8000 WebSocket. No LCW or Microsoft navigation/login was used for this test; the normal LCW configuration was restored afterward, with Chromium closed.

Focused offline checks: 74 distinct tests passed (72 existing checks plus two new SPA/security checks), one optional PostgreSQL test skipped; the production frontend build passed. The actual Docker PostgreSQL persistence was separately verified as above. Real LCW authentication in the new Linux profile, Intel builds, and migration of host PostgreSQL history were not verified or performed. Existing macOS browser authentication and host database files were left intact.
