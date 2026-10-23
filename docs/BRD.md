# LCG Command Center — Business Requirements Document (BRD)

**Version:** 0.3 (autonomous read-only inspection and authentication continuity)  
**Date:** 2026-10-23  
**Status:** Read-only automation implemented/offline-tested; real end-to-end acceptance pending; writes excluded  
**Project:** LCG — Learning Care Group Command Center  
**Initial use case:** Camila sample child-care subsidy contract setup on LCW

## 1. Business context

The supplied project brief describes Learning Care Group (LCG) as operating approximately 1,100 schools in about 40 U.S. states. School directors perform repetitive administrative tasks in external systems, including enrollments, attendance and subsidy claims. The proposed Command Center would eventually consolidate such work. This mini-hackathon addresses **only subsidy/admission contract setup in LCW**; it does not implement the broader operational platform or the separate churn use case.

The brief and demo describe a director who supplies a document and instructs an AI coworker to set up the corresponding contract in LCW. The required LCW workflow lacks an available integration API, so the solution must use authorized browser interaction.

The current iteration uses actual LCW preproduction through local headed Chromium controlled by Python Playwright. It reuses the existing FastAPI, React and PDF extraction implementation. Scope is limited to establishing the session and read-only exploration; contract/PRA changes and saves are excluded. A mock application or remote-browser infrastructure is not part of this iteration.

## 2. Business problem

- A director manually transfers details from a source document into LCW across multiple screens.
- Manual navigation, re-entry and verification consume staff time and introduce opportunities for mistakes and duplicate records.
- Users must also handle access challenges, including sign-in and MFA, while retaining control over consequential changes.
- Fresh browser sessions cause avoidable repeated MFA interruptions. The Command Center must reuse its dedicated local browser profile and active session, while respecting Microsoft/LCW authentication expiry and security policy.
- The director currently lacks one place to review the original document, agent progress, pending approvals and the target LCW portal.

**Baseline efficiency, error rates and savings are not provided.** They must be measured; no ROI claim is assumed.

## 3. Business objectives

| ID | Objective | Business result sought |
|---|---|---|
| BO-01 | Reduce repetitive data entry | Less director interaction for a valid contract |
| BO-02 | Improve correctness | Contract fields match reviewed source data and approved overrides |
| BO-03 | Preserve human authority | Human signs in, resolves ambiguity and approves submission |
| BO-04 | Make execution observable | Visible LCW session, clear step/status tracking and actionable errors |
| BO-05 | Support repeatable operations | Auditable runs, error recovery and future deployment on Azure |

## 4. Stakeholders and responsibilities

| Stakeholder | Expected responsibility | Status |
|---|---|---|
| School director / trained operator | Upload document, review fields, authenticate, approve changes | Identified by brief |
| LCG product/business owner | Confirm workflows, field mapping, authorization and acceptance | Specific person TBD |
| LCW system owner / admin | Grant authorized preproduction access, explain environment constraints | TBD |
| Engineering team | Build command center, workflow, browser adapter and Azure deployment | Project team |
| Security/privacy reviewer | Approve handling of children/family data, retention and access | TBD |

## 5. In-scope business capabilities (first release)

The following list is the intended first release, not a completion report. Current delivery comprises extraction/source review, a single owned persistent Chromium session, autonomous ID-based child lookup, existing contract/PRA list inspection and source-to-portal comparison. A synthetic PDF-to-comparison test exercises the workflow offline. The actual Camila acceptance flow remains unverified; editing, approvals and saved-result verification remain future capabilities.

1. Upload an authorized sample contract PDF.
2. Extract and present structured contract details with original-source provenance.
3. Permit deliberate edits in natural language or a review form, recording approval and change history.
4. Start and observe an authorized LCW browser session within the Command Center.
5. Pause for sign-in/MFA and allow user takeover of that exact session.
6. Locate and verify an **existing** child/family before making changes.
7. Configure contract agency, dates, reimbursement and billing fields, subject to discovered LCW field mapping.
8. Detect an applicable existing PRA (program rate agreement / schedule record in the provided materials); reuse it when appropriate rather than creating a duplicate.
9. Present a before-save summary; proceed only on explicit operator approval.
10. Verify the actual saved result using LCW confirmation/record detail; present the reference number if assigned.
11. Record status, non-sensitive action logs and failures for review.
12. Reuse the same local browser session/profile across steps and restarts to avoid unnecessary sign-in prompts. Pause for manual MFA only when required by the identity provider or portal; do not promise that MFA can be eliminated.

## 6. Out of scope

- Churn prediction, attendance automation, subsidy claims processing and organization-wide rollout.
- Creating brand-new family/child accounts unless separately approved and specified.
- Bypassing identity controls, CAPTCHA or MFA; credential scraping or adding password storage. Existing credentials are read from `backend/.env`; the dedicated local browser profile retains sensitive authentication state and must remain private and excluded from version control.
- Changing government policy, determining legal eligibility, or calculating official reimbursement from the fictional example.
- Production submissions based on the explicitly marked non-production Camila training document.
- Bulk processing or unattended production writes in the initial release.

## 7. Reference workflow from demo and document

The demo presents six activities: extract fields, verify child in LCW, set up contract, save contract and unit prices, set up PRA, save PRA. It shows user takeover at Microsoft sign-in/MFA and explicit approvals. The supplied training PDF instead says **reuse an existing PRA (2026-08-08 through 2026-11-20)**. Therefore, the implementation must branch based on actual LCW state and document requirements, not blindly follow demo clicks.

The sample PDF identifies **Camila Barajas**, child ID **1155030**, family ID **870985**, center **0265-CA Jurupa Ave**. Source contract dates are **2026-09-28 to 2026-10-02**. The demo includes an example user-approved end-date change to **2027-02-10**. Such a change must be recorded as an override, and the agent must flag that it exceeds the supplied PRA period rather than silently treating the PRA as valid for the entire contract period.

The training document says the agency/rates are fictional and are not a real authorization. See the provided sample PDF for full training terms.

### Actual read-only findings — 2026-10-22

Microsoft sign-in and operator-completed MFA led to the authenticated LCW portal. Interactive Python Playwright exploration verified **child ID 1155030**, **family ID 870985** and **Family Center 0265-CA Jurupa Ave** on Camila's child detail page.

LCW already showed an active School Age 2 assignment dated **2026-08-08 to 2026-11-20**, matching the sample's PRA period, and an existing third-party contract with **CA - ST Riverside County Office of Ed**, dates **2026-09-28 to 2026-10-02**, contract ID **1833773**. Full PRA compatibility and contract/rate/copay correctness were not verified. The next contract workflow must inspect and reconcile these records before proposing any creation, amendment or reuse. No LCW record was changed or saved.

The live preview and takeover were implemented but authenticated integration verification was left pending at the operator's request. A second isolated test browser caused another MFA prompt and was closed. The app now retains its single local Chromium process across API shutdown, reconnects to the exact owned tab and reuses its fixed profile. These lifecycle guarantees are offline-tested; real restart/authentication behavior remains unverified. No active controlled session was available on 2026-10-23, and no new browser/login/MFA was initiated. Do not trigger further MFA for tests without renewed operator direction. See [LCW browser discovery](LCW_BROWSER_DISCOVERY.md).

## 8. Business rules and controls

| ID | Rule |
|---|---|
| BR-01 | Never persist a contract against a child/family solely on a fuzzy name match; validate identifiers and center, or stop for review. |
| BR-02 | Keep extracted source values distinct from edits, inferred values and values observed in LCW. |
| BR-03 | Missing, contradictory, unsupported or policy-sensitive values require user clarification. |
| BR-04 | Check LCW for existing contract/PRA data before creating potentially duplicative records. |
| BR-05 | Do not create a duplicate PRA when the matching record should be reused. |
| BR-06 | If dates, rate tier, hours or eligibility disagree, pause and ask the director; do not invent policy. |
| BR-07 | Save/submit and destructive/financially consequential changes require explicit human approval. |
| BR-08 | The agent stops interacting while the user has browser control; resume only after control is returned. |
| BR-09 | Verify writes against LCW after save. Never report success solely because a button was clicked. |
| BR-10 | Use synthetic or approved preproduction data until access/privacy/permission controls have been validated. |
| BR-11 | Reuse the active browser and dedicated local profile; do not open fresh browser sessions for individual steps or routine validation. Never bypass MFA or change identity-provider policy to avoid prompts. |
| BR-12 | Reconcile the observed existing contract and PRA before proposing creation. Any action that may create a duplicate PRA or modify saved LCW data requires explicit approval. All such writes remain outside this iteration. |

## 9. Proposed success measures (targets to agree, not baseline facts)

- **Functional completion:** one authorized Camila test case completes a verified save with correct record association and proper approval handling.
- **Field correctness:** all mandatory fields in a curated test fixture are extracted or explicitly marked unresolved; zero silent substitutions.
- **Safety:** zero unapproved writes in automated tests; zero duplicate PRA creation in the existing-PRA test.
- **Recoverability:** injected session expiry, MFA prompt, missing field and ambiguous record lead to bounded pause/recovery rather than silent continuation.
- **User visibility:** operator can see current step, error reason, and browser state at each checkpoint.
- **Efficiency (measure after prototype):** compare human actions and elapsed time versus a manually timed baseline; do not invent targets prematurely.
- **Authentication continuity:** repeated actions use the same browser; a restart reuses valid authentication when permitted. This must be tested when the operator is ready, without opening extra test browsers or changing MFA policy.

## 10. Dependencies, risks and assumptions

| Item | Impact / mitigation |
|---|---|
| LCW preproduction environment, credentials, permission | Authorized real read-only access succeeded on 2026-10-22. Use the configured environment; record changes remain outside this iteration. |
| LCW terms/technical protections | Confirm authorized automation and access scope before connecting. |
| MFA and session expiration | Reuse one dedicated local profile. Pause for required manual authentication; session persistence does not override Microsoft/LCW policy. |
| LCW form labels/validation | Login, search, child detail and record-list navigation are documented from observation. Full contract/rate/copay field mapping remains unverified. |
| PII about children and families | Limit scope, encrypt, apply least privilege, short retention and controlled access. |
| Existing PRA versus new PRA | Inspect before create; resolve date coverage and conflicting inputs with human review. |
| Azure tenant/resources/region | Deployment depends on access, budget, security region and capacity choices. |
| Demo versus sample discrepancies | Preserve both as references; no automatic reconciliation without business sign-off. |

## 11. Delivery modules

1. **Documentation & architecture:** PRD, BRD and TRD; agreed scope/decisions.
2. **Foundation & Command Center:** manual local setup, React UI, FastAPI, upload and status.
3. **Document intelligence & workflow:** extraction, validation, chat commands, LangGraph checkpoints.
4. **Browser agent & LCW:** local Playwright session, actual read-only discovery, same-session preview and local takeover; later add validated lookup, explicit write approvals and saved-result verification.
5. **Hardening & Azure:** security tests, audit, Docker, cloud deployment and end-to-end checks.

For the current iteration, do not install dependencies, create environment files or execute Git operations. Use existing components and keep new files essential; additional agents, databases, services and infrastructure are outside scope.

## 12. Acceptance and unresolved decisions

Business acceptance requires: a visible source-to-LCW mapping; a verified existing family/child; an appropriate existing-PRA branch; operator-controlled sign-in and final write approval; a saved-result receipt or explicit failure; and a demonstrable end-to-end run on authorized test data.

That is future full-release acceptance. Historical evidence establishes real read-only access and identity/existing-record discovery. Current automation reports observed values separately from PDF values using Matching, Different, Missing and Not verified; unknown unit prices/copays/program/schedule details cannot be presented as matches. Multiple/ambiguous records require review, and an unreadable table must not be treated as confirmed absence. Module 4 passed 70 backend tests, TypeScript and frontend build/lint; these checks do not prove authenticated profile reuse, live-preview/takeover integration or a completed contract setup.

**Resolved by observation:** configured preproduction access, Microsoft sign-in, Camila's identity and the presence of an existing assignment/contract. **Still requiring validation or decision:** autonomous real Camila upload-to-comparison acceptance and actual browser persistence; full contract field map; compatibility/reconciliation of existing records; PRA coverage of any proposed extension; approval boundaries; who may access submitted files and retained browser profiles; cloud region and rollout; business success measures. No further login/MFA test is requested now.

---

**Source basis:** user-provided problem statement, earlier demo/PDF descriptions and the actual read-only LCW exploration recorded in [LCW browser discovery](LCW_BROWSER_DISCOVERY.md). The original PDF/video were not available for fresh inspection in this iteration. Future deployment and policy choices remain proposals.
