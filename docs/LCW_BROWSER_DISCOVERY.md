# LCW browser integration — Module 4 update, 2026-10-23

This iteration supersedes the earlier mock-first guidance in PRD/BRD/TRD for the explicitly authorized real preproduction exploration. No dependencies, environment files, databases, agents, remote browser services or Git operations were added/run.

## Historical real exploration — 2026-10-22

Python Playwright launched headed Chromium and opened the `LCW_URL` from `backend/.env`: `https://lcw2-preprod.learningcaregroup.com/center/daycare.do`. Microsoft sign-in redirected through `login.microsoftonline.com`. The configured username and password were submitted, then automation stopped at **Approve sign in request**. The operator completed MFA. At **Stay signed in?**, **No** was selected in this initial temporary exploration session. Authentication succeeded and the actual LCW home screen loaded.

Observed and used selectors (Python Playwright; names are exact unless noted):

| Screen | Observed selector / navigation | Verification |
|---|---|---|
| Microsoft Sign in | `locator('#i0116')`, button `Next` | Username step completed |
| Microsoft Enter password | `locator('#i0118')`, button `Sign in` | Password submitted once |
| MFA | heading `Approve sign in request` | Paused; operator completed it |
| Stay signed in? | button `No` / `#idBtn_Back`; button `Yes` also present | No selected in temporary discovery session |
| LCW home | `/center/daycare.do`; `#userProfileMenu`; navigation link `Family` | Authenticated portal reached |
| Center selector | combobox selected option `0265-CA Jurupa Ave` | Correct center observed; not changed |
| Family menu | navigation link `Family` | Expands `Group PRA Changes`; not the child lookup path |
| Search menu | `get_by_role('link', name='\uf002', exact=True)` | Opens alphabetical family search |
| Family search | button `A - B` | Loaded `Family Search Results (A - B)` |
| Search result | `a[href="cgchild.do?child_id=1155030"]` | Camila Barajas; adjacent Barajas family ID 870985 |
| Child detail | `/center/cgchild.do`; tab `Admission/Personal` | Explicit Child/Student ID 1155030; Family ID 870985; Family Center 0265-CA Jurupa Ave |
| Existing PRA | tab `Program/Room Assignment` | Existing active classroom assignment read |
| Family contracts | tab `Third Party` → `/center/familyThirdParty.do` | Family ID and center retained |
| Camila contract list | `a[href="javascript:viewContracts(1155030,'Barajas, Camila')"]` | Read Third Party Contract List for Camila |

The search icon's CSS text locator failed because its visible glyph is not ordinary link text; the accessible-role selector succeeded. Dynamic `sm-…` menu IDs change across navigation and must not be hardcoded. Clicking the top `Search...` textbox alone did not load results. Text search and Advanced Search were not tested. The full query parameters of search-result navigation were not captured; do not invent them or navigate directly to a guessed search URL.

### Identity and existing records

Camila was verified using **child ID 1155030**, **family ID 870985**, and **Family Center 0265-CA Jurupa Ave** on the child detail page, in addition to her name. This is not a name-only match.

The Program/Room Assignment tab showed one active assignment:

- School Age 2 ~ 0265-CA Jurupa Ave.
- Effective 08/08/2026; end 11/20/2026.
- Displayed rate: SU County EE School Age-B/A ($170.00).
- A PDF link contains `childId=1155030`, `childDayId=7880246`, `roomId=19731`. The PDF and edit control were not opened.

These dates match the existing-PRA period described by the sample. This establishes an existing assignment, **not** full compatibility of every schedule/rate/copay field. Reuse remains subject to reviewing its details; never create a duplicate just because the video creates one.

The Third Party Contract List already contained:

- Agency: CA - ST Riverside County Office of Ed.
- From 09/28/2026 to 10/02/2026.
- Contract ID 1833773, observed in the existing `editContract(1833773)` link.

Neither edit nor copy nor add was clicked. The matching agency/dates require reconciliation before any future creation. Contract contents, unit prices, copays, and persisted field correctness have not been verified. No contract or PRA was created, modified, submitted or saved in this exploration. No personal, financial or schedule form input was changed.

## Module 4 implementation — 2026-10-23

The existing backend reported no active controlled browser during the status-only check on 2026-10-23. **No new Chromium, LCW login or MFA was initiated.** The historical observations above guide navigation; they are not fresh live verification of the new autonomous workflow. The original Camila PDF was unavailable for a fresh end-to-end upload test. Automated verification used synthetic PDFs and mocked Playwright pages.

### One persistent browser and explicit authentication

`backend/app/browser.py` now owns one local Chromium process using the fixed `backend/.lcw-browser-profile/`. An OS advisory lock prevents separate backends from claiming it concurrently; the in-process lock serializes start and browser actions. Chromium's existing profile lock is respected and never removed; no alternate or temporary profile is used.

To preserve the open browser through backend shutdown, Chromium is launched as a detached local process using the Playwright-installed executable. Playwright attaches to its one default persistent context through loopback-only CDP. Private metadata inside the ignored profile records the process and exact controlled target. Reconnection verifies that process owns the configured profile and resolves the recorded target; it does not pick another tab or navigate an authenticated page. This is a local browser-lifecycle implementation, not a remote-browser service. CDP addresses and raw protocol access are not exposed through application APIs.

Routine API shutdown cancels automation and disconnects its transport; it does not close Chromium or log out. Only an explicit Close action issues a browser-close command. Neither close nor disconnect clears profile, cookies or local storage. Authentication reuse across real browser/backend restarts still needs live verification. Browser launch/attachment through the new local-process mechanism has only been exercised with mocks because another browser/session is not authorized for testing now.

Status checks, uploads, UI reconnects and inspection retries never sign in. The **Reconnect existing browser** action can attach only to an already owned process; it never launches one. **Open dedicated browser** is a separate explicit action. **Sign in explicitly** is the only credential-submission action, with one attempt per browser lifetime, recorded in private metadata. An authenticated session returns without entering credentials. MFA/unknown sign-in requires the operator in the same native window. No login action was invoked for this implementation's tests.

### Autonomous read-only inspection

The backend retains the latest validated extraction in memory with an opaque extraction ID. The inspection request supplies that ID rather than editable identity fields. Each job holds an immutable source snapshot; another PDF upload cannot silently change its comparison. UI refresh retrieves the existing document and job; it does not restart or cancel browser execution. Backend-process restart retains Chromium but not in-memory PDF/results/jobs; upload again and explicitly start a new inspection.

The job performs the following allowlisted actions from observed LCW navigation:

1. Verify the configured LCW origin plus the observed profile/menu markers, with no login attempt.
2. Open the search-icon link, then the alphabetical family group derived from the PDF family name.
3. Read actual search-result links and select a unique numeric child-ID target, confirming the name. Duplicate names with a different ID do not qualify; duplicate matching targets stop inspection.
4. Open only the observed child-detail link. Independently compare visible Child/Student ID, Family ID, Family Center, first name and last name with the source.
5. Open Program/Room Assignment; recheck family and child context, then parse existing classroom, center, dates and the displayed rate description. A rate description is not treated as a verified program, tuition, reimbursement unit price or copay.
6. Open Third Party, then the actual child-specific `viewContracts` control. Verify its exact child-specific list heading and family context. Read contract IDs from existing edit-link targets without clicking them, plus the agency and date columns.
7. Compare all relevant source fields with observed values. Display **Matching**, **Different**, **Missing** or **Not verified**, preserving source text/page and portal evidence. Future approved changes remain null. PRA coverage compares the observed assignment period with the source contract dates; it does not establish complete rate/schedule compatibility.

No hardcoded child, family, contract or PRA IDs are used by the agent. `backend/app/inspection.py` separates pure parsing/comparison from lifecycle and browser dispatch. The existing extraction parser and React review remain in use.

Multiple contracts/assignments are all reported. A unique exact source date match (plus agency for contracts) may identify the relevant record; otherwise no first-row selection is assumed. An unreadable/empty table is **not verified**, because the portal's empty-state selectors have not been observed. The comparison supports Missing for confirmed absence, but it does not infer absence from an unrelated global “0 records” label.

### Scope of observed fields

The verified screens expose identity, contract ID/agency/dates and assignment classroom/dates/displayed rate description. The agent does not open edit, copy, add or save controls. Contract reimbursement settings, unit prices, copays, program and detailed attendance times remain **Not verified** unless a later authorized read-only discovery establishes their actual controls. No fabricated values or results are substituted.

### Progress, takeover and recovery

The backend owns a single background inspection task; duplicate Inspect requests for its source reuse that job. An idempotency key prevents repeating a completed request after a lost response. Progress events are emitted at actual operation boundaries; no timed/fabricated steps are displayed.

Take Control immediately changes ownership and the cancellation epoch without waiting for the entire task. The current bounded read/navigation may finish (12-second operation limit); the next action cannot start. Return Control independently checks authentication and restarts the inspection from search/identity verification, so native user navigation cannot silently resume against old observations. Stop Inspection cancels the task while retaining the browser and PDF. Network timeouts, missing controls, mismatched identifiers, lost tabs, unknown pages and session expiration either pause or fail with fixed, actionable messages. There are no automatic navigation or login retries.

States include idle, authenticated, awaiting_authentication, awaiting_mfa, inspecting, paused, failed, cancelled and completed. A completed comparison can contain unverified fields; it does not mean contract/PRA setup or full compatibility is verified.

### API and preview

All browser routes and extraction require the local request guard (loopback host/client, `X-LCG-Local: 1`, allowed frontend Origin when supplied). These are development controls, not a production multi-user authentication system.

- Existing `/api/browser/start`, `/login`, `/status`, `/frame`, `/navigation`, `/take-control`, `/return-control`, `/close` remain.
- `POST /api/browser/reconnect`: existing owned process only, no launch/navigation/login.
- `POST /api/browser/inspect`: validated extraction ID and request ID; starts one server-side read-only job only when already authenticated and under agent control.
- `GET /api/browser/inspection`: current progress, source snapshot, observed records and comparison result.
- `GET /api/browser/document`: recover the in-memory extraction after UI refresh.
- `POST /api/browser/cancel-inspection`: stop automation and retain the session.

Screenshots come from the exact controlled page, remain in memory and are returned with no-store and their actual capture timestamp. Capture requests are serialized/deduplicated; frames are discarded if navigation changes authentication or the page during capture. Sign-in and unfamiliar pages suppress preview. The image remains view-only; native-window control is required. The UI polls while visible with bounded requests and slows while hidden. Public arbitrary URL, JavaScript, CDP, input and record-write endpoints do not exist.

## Verification status — 2026-10-23

- Offline: **70 backend tests passed** at the final implementation check; TypeScript, production build and lint passed. Existing PyMuPDF SWIG deprecation warnings remain. Tests cover synthetic PDF → real inspection orchestration with a mocked DOM → identity verification → contract/PRA parsing → comparison, wrong identifiers, duplicate/multiple records, immutable source snapshots, cold/concurrent starts, fixed profile and ownership, exact-tab CDP reconnection, preserved browser on API shutdown, MFA/expiry pauses, takeover/resume/cancel, refresh during a running job, sanitized failures, stale-header session-expiry detection, lost-transport ownership cleanup and same-page frame handling.
- Local API smoke: health returned 200; the local-request guard returned 403 without its header; Inspect without an extraction returned 409. Status remained idle with no browser/job before and after these checks.
- Live today: status-only check found no active managed browser. No portal navigation, fresh contract/PRA verification, screenshot capture or login/MFA was performed.
- Still unverified: launch/reattachment and persistent authentication with actual Chromium under the new lifecycle; actual selector timing/table parsing for the autonomous run; authenticated preview and takeover; the original Camila upload-to-result acceptance flow.
- The milestone is **implemented and offline-tested, not fully verified against LCW**. No LCW data was changed.

Use one existing backend worker without reload: `.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --no-access-log`. Start the existing frontend normally. Reconnect an existing owned browser when available; do not initiate login/MFA merely to complete a test.

References: [Playwright CDP attachment](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp), [authentication state sensitivity](https://playwright.dev/python/docs/auth), [Microsoft session lifetime](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-session-lifetime).

## Step 1 — Interactive embedded browser (2026-10-23)

This section supersedes the screenshot-only implementation above. The React panel now receives CDP screencast frames over one authenticated local WebSocket and sends a finite set of mouse, wheel, text, keyboard and history actions to the exact page used by inspection. Frames stay in memory; there is no screenshot polling endpoint, iframe, second portal browser, arbitrary JavaScript/CDP endpoint, or frame recording. Rendering is capped at 20 fps with acknowledgements and a one-frame pending queue. The fixed 1280×800 viewport scales with the panel and expanded view.

Human input and agent inspection share the backend operation lock. Take Control invalidates the agent epoch immediately; input waits until the current bounded operation finishes. Return Control re-observes authentication and restarts inspection with identity verification. A viewer lease prevents simultaneous viewer connections; stale ownership epochs reject queued input. Loopback, Host, Origin, the existing local HTTP header and short-lived single-use viewer tickets restrict access. Tickets travel in the initial socket message, not URLs or storage. Payload validation errors are sanitized. Frames, input values, tickets and protocol errors are not logged or persisted by the application.

New launches use headless Chromium and the unchanged fixed `.lcw-browser-profile` path. Existing owners are attached without being closed or relaunched. Profile locks and process ownership checks remain in force. Explicit close waits for an owned child process to exit; shutdown only detaches. The legacy login action now hands control to the operator and never submits configured credentials. Microsoft authentication and MFA remain manual. Navigation is limited to the configured HTTPS LCW origin and exact supported Microsoft origins; popups and downloads are blocked. A restricted LCW home button recovers from blocked-navigation error pages.

### Controlled migration

Reconnect first. An existing headed browser remains headed and is not forcibly closed. When the operator is ready, stop inspection, select **Close browser explicitly**, wait for closure, then **Open embedded browser**. The same profile is retained and the next launch is headless. Never delete locks, copy/migrate the profile, launch a second process against it, or retry MFA to validate this change. If ownership cannot be verified, resolve the existing owner before reopening.

### Verification and limitations

- 76 offline backend tests pass, including PDF extraction, inspection orchestration and comparison regressions, input validation/scaling, ownership boundaries, navigation restrictions, ticket access, duplicate ownership, and streaming cleanup.
- The opt-in real Chromium acceptance test passes against the actual React app at localhost:5173 with a separate isolated test backend. Verified streamed image decoding; clicks; typing; Tab, Enter and Backspace; wheel scrolling; panel resizing; expanded view; back/forward/reload; frontend refresh; socket disconnection/recovery; human/agent handoff on the exact same page; synthetic MFA visibility; blocked-navigation recovery; backend detach/reattach; duplicate-owner prevention; and synthetic cookie/local-storage retention after explicit close/reopen.
- Synthetic tests use named reusable profiles under `/tmp/lcg-step1-tests`, never the LCW profile or `.env`. Portal and Microsoft fixture requests are fulfilled locally. Test browser processes are explicitly closed. No real LCW authentication or MFA was initiated.
- Frontend lint, TypeScript checks and production build pass. Existing PyMuPDF SWIG deprecation warnings remain.
- Real LCW session persistence and Microsoft policy compatibility are **not verified**. At initial inspection, the managed profile directory was absent and no process owned that path; unrelated existing Chromium windows were untouched. Synthetic persistence does not prove existing LCW authentication will survive headless migration.
- Native OS authentication dialogs, security-key prompts, separate-window flows, downloads, drag selection and full IME composition are not implemented. Unsupported identity-provider origins remain blocked rather than bypassing Microsoft policy. Only one Command Center viewer can connect at a time.

Run offline checks from `backend`: `../.venv/bin/python -m pytest tests -q`.
With the frontend already running, opt into the isolated live test: `LCG_BROWSER_TEST=1 ../.venv/bin/python -m pytest tests/test_browser_live.py -q`.
Run frontend checks from `frontend`: `npm run lint`, `npx tsc -b --pretty false`, `npm run build`.
Run one backend worker without reload: `.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --no-access-log --ws-max-size 16384`.
No dependencies or environment files were added. No work on Steps 2–5 was performed.

## Step 5 — preparation implemented; writes blocked

Chat now exposes `prepare_contract_setup`, which reuses the existing session and read-only inspection. It independently checks child/family/center identity, prepares a six-step review plan, preserves PDF provenance and warnings, lists proposed values and unknown current values, identifies existing contracts for reconciliation, and flags PRA reuse and coverage conflicts. The blocked plan is retained in the existing workflow result and chat tool record. Refresh/restart cannot authorize or replay a save.

This is **not complete contract automation**. The reference video is unavailable in the workspace. The discovery above did not observe contract/PRA edit forms, rate/copay controls, save controls or post-save read-back selectors. No write implementation, actionable approval or automatic correction application is enabled. Existing generic approval records cannot authorize this plan. Chat corrections remain proposals requiring confirmation; they do not alter PDF values or LCW. No selectors or field values were fabricated. No LCW session, login or write was initiated to test this step.

To finish: inspect those forms in the existing operator-authenticated embedded session, reconcile the video and supplied PDF with current records, bind each exact change to an immutable approval and fresh identity/record checks, then implement serialized saves and independent post-save rereads. Any real write verification requires specific operator authorization; approval alone must not bypass unresolved PRA coverage or missing selectors.

## Step 5A — discovery blocked (2026-10-23)

The running local backend's browser status returned `state: idle`, `active: false`, `authenticated: false`, `owner: agent`, `busy: false`. No active authenticated controlled page was available through the application. Per the discovery restrictions, work stopped at this prerequisite check; no browser launch, login/MFA, record navigation, form interaction or save was attempted. This status does not establish whether a detached Chromium process still exists.

No new fields, selectors, dropdown options, validation behavior, edit navigation or post-save verification paths were verified. The historical findings above remain historical. Contract/PRA form discovery requires the operator to restore the existing authenticated session in the Command Center before another discovery run.

On the subsequent operator-requested continuation, the local backend again reported no active authenticated page. The existing-only `/api/browser/reconnect` action was attempted (its code path forbids launching a replacement browser). A follow-up status check still returned `idle`, `active: false`, `authenticated: false`, with “No active controlled browser. Inspection requires an existing authenticated session.” No login, MFA, record navigation or form interaction was attempted. No additional selectors were verified; the session visible to the operator is not currently available through this backend's controlled page.
