# LCW local browser integration — 2026-10-22

This iteration supersedes the earlier mock-first guidance in PRD/BRD/TRD for the explicitly authorized real preproduction exploration. No dependencies, environment files, databases, agents, remote browser services or Git operations were added/run.

## Verified real exploration

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

## Implemented foundation

`backend/app/browser.py` adds one local session to the existing FastAPI app. Configuration is read from `backend/.env` using `LCW_URL`, `LCW_USERNAME`, and `LCW_PASSWORD`. Browser exceptions are replaced with fixed messages. No raw DOM, tokens, credentials or URL queries are returned by the API. Local browser routes require a loopback client/host and `X-LCG-Local: 1`; browser origins are restricted to the two existing development UI origins.

- `POST /api/browser/start`: open/reuse the dedicated headed Chromium context and page. Repeated starts retain the active page rather than relaunching or navigating away.
- `POST /api/browser/login`: observed Microsoft username/password steps only; one password attempt per launched session. MFA and other prompts remain manual.
- `POST /api/browser/take-control`: pause sign-in automation and focus the same local window.
- `POST /api/browser/return-control`: re-observe before allowing sign-in automation. MFA still forces human ownership.
- `POST /api/browser/close`: close Chromium gracefully; retain the dedicated profile.
- `GET /api/browser/status`, `/frame`, `/navigation`: state, a real JPEG from the controlled page, and visible link labels. No generic click, arbitrary URL, script, record edit or save endpoints exist.

Navigation timeouts, closed pages, unfamiliar pages and return to Microsoft sign-in are surfaced rather than reported as success. Authentication requires the configured LCW origin plus the observed profile/menu elements. Child identity is not inferred from authentication. Automated child lookup/verification and PRA/contract inspection are **not yet exposed as application actions**; the actual exploration above was performed interactively with Python Playwright.

### Avoiding repeated MFA

The initial discovery and integrated testing used two isolated temporary contexts, which caused a second MFA prompt. The operator declined further MFA testing. That second browser was closed; the first authenticated exploration session was left intact.

The implementation now uses `launch_persistent_context` with the fixed directory `backend/.lcw-browser-profile/`. It reuses the same context while running and the same profile after restart. The directory is excluded in `.gitignore` and created with owner-only access. It contains sensitive authentication/browser data; it is not an environment file and must not be shared or committed. Closing the browser is **not** signing out. No fresh-profile fallback is attempted if the profile is locked by another process. Run a single backend worker without auto-reload during a browser workflow.

This change cannot retroactively turn the earlier temporary context into the dedicated profile. The dedicated profile may need an initial sign-in when the operator is ready. Do not open extra authenticated test browsers or automatically re-submit credentials while MFA is pending. Microsoft/LCW can still expire or revoke sessions, and session-only cookies may not survive browser closure. The operator decides any **Stay signed in?** prompt; the agent does not change MFA or identity-provider policies. Cross-restart authenticated reuse remains untested.

## Minimum same-session visualization

The React browser panel polls the existing FastAPI server for a JPEG approximately every 1.5 seconds. `page.screenshot()` reads the exact Playwright page, with no unrelated iframe, separate LCW login, VNC, remote worker or browser infrastructure. Frames are sent with `Cache-Control: no-store`; sign-in/unrecognized pages suppress the preview; frames are not written to disk. This is a view-only, low-frame-rate local preview, not a full desktop video stream. Page data can still appear on screen as intended for the operator.

Current **Take control locally** pauses sign-in dispatch under the same server lock and brings Chromium forward. It does not relay mouse/keyboard input from the React image. Direct use of the native browser remains possible, so the application cannot prevent an operator manually editing LCW there. The read-only constraint applies to implemented agent actions.

For future in-panel control, use the existing Chromium CDP session for `Page.startScreencast` and frame acknowledgments, with narrowly validated mouse/key input on the same page. Require a server-held human-control lease, coordinate scaling, session identity and expiry, and re-observation on return. Do not expose the debugging port or arbitrary CDP commands. That upgrade is investigated only; it is not implemented or tested.

References: [Playwright persistent contexts](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context), [page screenshots](https://playwright.dev/python/docs/api/class-page#page-screenshot), [CDP sessions](https://playwright.dev/python/docs/api/class-cdpsession), [CDP Page](https://chromedevtools.github.io/devtools-protocol/tot/Page/), [Microsoft session lifetime](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-session-lifetime).

## Verification boundary and local use

Verified against real LCW: headed launch, Microsoft credential flow, manual MFA handoff, authenticated home, alphabetical family search, child/family/center identity, existing PRA row, and existing contract list. The integrated FastAPI launch/login routes also reached MFA; post-MFA live preview and takeover verification were deliberately left pending at the operator's request. No additional MFA will be initiated for testing in this iteration.

Offline checks cover extraction, health, local API restrictions, rejection of record writes, reuse of the active page, stable persistent-profile path, manual-control lock, no password retries, redaction and timeout handling. These tests use synthetic data/mocks, not an LCW mock application; they do not prove real portal behavior or session persistence.

Validation on 2026-10-22: all **33 backend tests passed**; frontend production build and lint passed. PyMuPDF emitted existing SWIG deprecation warnings. The API was restarted with profile reuse enabled without opening a browser or initiating another sign-in.

Start the existing API from the repository root with `.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --no-access-log` (one worker, no reload). Run the existing frontend with its normal `npm run dev` command. Use **Open LCW** only when ready for the first sign-in in the dedicated profile; later starts reuse it. Do not start a second backend/browser for validation. The sample PDF and reference video were not present in this workspace during this iteration; their business facts above come from the supplied PRD/BRD/TRD, not a newly processed original attachment.
