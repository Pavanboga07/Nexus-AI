# Phase B — Frontend Product Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "settle 2 things before lunch" fully doable in the UI: ask a person, see what's waiting on you and on others, approve with evidence, never get stranded by an expired session.

**Architecture:** Fix in place with shared UI primitives first (`ErrorState`, `ConfirmModal`, `useAsync`), then per-page adoption. No response-shape changes needed — all fixes are reads the backend already returns. The M10 contract test (`tests/test_m10_frontend_contract.py`) must stay green after every task; extend it where a new gap is closed.

**Tech Stack:** Next.js 14 App Router, React 18, Tailwind, TypeScript. Verify with `npx tsc --noEmit`, `npm run build`, and the M10 suite. (No frontend unit runner exists — verification is typecheck + build + contract test + manual click-through.)

---

## Ground rules (every task)

- No new npm dependencies (QR/deadline formatting: use copyable URLs + existing `formatDate` in `lib/utils.ts`).
- Never show an ID where a name exists; never show "verified" unless the signature verified; every async action shows its state.
- After each task: `npx tsc --noEmit` clean, `npm run build` clean, `pytest tests/test_m10_frontend_contract.py -q` green, commit.

## File map

| File | Responsibility after this phase |
|---|---|
| `src/lib/api/client.ts` | 401 → `/login` redirect, shared error envelope |
| `src/components/ui/ErrorState.tsx` (new) | Error banner with retry, reused by all pages |
| `src/components/ui/ConfirmModal.tsx` (new) | Inline confirm replacing `confirm()`/`prompt()`/`alert()` |
| `src/lib/api/approvals.ts` | Correct orchestration mapping; outbox + recent-decisions sources |
| `src/app/(app)/inbox/page.tsx` | Three panes: Needs you / Waiting on others / Done today |
| `src/app/(app)/chat/page.tsx` | Ask-@person picker, rollback on failure, evidence expander |
| `src/app/(app)/people/page.tsx` + `src/app/(app)/people/[id]/page.tsx` (new) | Connect-from-search, person detail |
| `src/app/(app)/agent/page.tsx` | Invite card URL + copy; capability-panel error state |
| Detail pages (tasks, workflows, autonomy, permissions, tools, activity, memory) | Real error states, busy states, no native dialogs, names not IDs |

---

### Task B0: Baseline

- [ ] **Step 1: Record green.** `npx tsc --noEmit`, `npm run build`, `$env:NEXUS_ALLOW_NO_DB="1"; python -m pytest tests/test_m10_frontend_contract.py -q` (from `D:\AI nexus\nexus`). Save outputs. All green or stop.

---

### Task B1: Fix orchestration approvals (broken source)

**Files:** Modify `frontend/src/lib/api/approvals.ts:140-190`, `tests/test_m10_frontend_contract.py:44-63`.

- [ ] **Step 1: Write the failing test.** Extend the approval-sources test to probe `/orchestration/runs` for 200-with-list-shape or 503-envelope (same standard as the other three sources), and add a mapping unit case: a backend run `{run_id, state: "waiting_approval", ...}` must surface as a pending approval item (today `loadOrchestration` reads `r.status`/`r.id`, which do not exist on `OrchestrationRunResponse`, so the source is permanently empty).
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement.** In `loadOrchestration`, read `state` (map `waiting_approval` → pending; `waiting_remote`/`executing` → waiting-on-others; terminal → done), key items by `run_id`, and verify the approve/reject bodies against `OrchestrationApproveRequest` in `app/api/routes/orchestration.py` before sending (fix the payload if it disagrees — read the schema first, do not guess).
- [ ] **Step 4: Re-run** contract test + typecheck + build — green. **Step 5: Commit.**

---

### Task B2: Session expiry never strands the user

**Files:** Modify `frontend/src/lib/api/client.ts`, `frontend/src/app/(app)/layout.tsx` or `AppShell.tsx` (whichever owns the shell — read first).

- [ ] **Step 1: Reproduce.** With the backend running, delete the session cookie, load `/inbox`; record the current behavior (error card, no path to `/login`).
- [ ] **Step 2: Implement.** In `apiFetch`, on 401: clear any local auth state and `window.location.assign("/login")` (full navigation, not router-push — state may be stale). Exempt the login page itself from the redirect loop.
- [ ] **Step 3: Verify.** Typecheck + build clean; manual: expire session on `/inbox` and on `/chat`, both land on `/login`. **Step 4: Commit.**

---

### Task B3: Shared primitives (ErrorState, ConfirmModal, useAsync)

**Files:** Create `frontend/src/components/ui/ErrorState.tsx`, `frontend/src/components/ui/ConfirmModal.tsx`, `frontend/src/lib/useAsync.ts`.

- [ ] **Step 1: Create `ErrorState`.** Props: `message: string`, `onRetry: () => void`, `hint?: string`. Renders the same alert styling the Inbox uses (`inbox/page.tsx` error alert) plus a Retry button. ~40 lines.
- [ ] **Step 2: Create `ConfirmModal`.** Props: `open`, `title`, `body`, `confirmLabel`, `danger?: boolean`, `requireReason?: boolean`, `onConfirm(reason?: string)`, `onCancel`. Covers every current `confirm()`/`prompt()` call site (memory delete, task cancel/reject-reason, workflow cancel, autonomy stop/approve-notes). ~90 lines.
- [ ] **Step 3: Create `useAsync`.** Returns `{data, error, loading, reload}` around a fetcher; `loading` starts `true` so first paint never says "No entries found" (fixes the `activity/page.tsx:33` false-empty). ~50 lines.
- [ ] **Step 4: Verify** typecheck + build. **Step 5: Commit.**

---

### Task B4: Adopt primitives + names everywhere

**Files:** Modify tasks, workflows, autonomy, permissions, tools, activity, memory pages; `people/page.tsx:78-96`; `inbox/page.tsx:193-198`; `approvals.ts:108-116`; `tasks/page.tsx:265-269`; `activity/page.tsx:95-98`.

- [ ] **Step 1: Names map.** Build `useContactNames()` (new, in `src/lib/api/people.ts` or a hook file): loads trusted agents once, returns `Map<agent_id, display_name>`, falls back to a short fingerprint slice (never a raw 24-char ID slice). Inbox titles/`requestedBy`, task peer lines, activity subtitles, and the People trusted list (add `@handle`, verification state, capabilities count from already-returned fields only — no new backend fields) all use it. Format Inbox expiry with the existing `formatDate`.
- [ ] **Step 2: Per page (repeat for tasks, workflows, autonomy, permissions, tools, activity, memory):** replace `console.error`-and-"No X found" with `<ErrorState onRetry={reload}>` via `useAsync`; replace each `alert`/`prompt`/`confirm` with `ConfirmModal`; add per-row busy/disabled state to every mutation button (revoke/remove/runner/cancel/stop). Acceptance per page: block the backend (stop it), reload the page, assert an error banner with working Retry (not an empty state); click each destructive action, assert a modal (no native dialog).
- [ ] **Step 3: Chat rollback + evidence.** In `chat/page.tsx:108-135`, remove the optimistic message in the `catch` path; add an evidence expander to inline approval cards (`chat/page.tsx:215-250`) showing category/purpose/action/expiry (same fields the Inbox shows at `inbox/page.tsx:163-199`); add loading/disabled to "New chat" (`chat/page.tsx:137-151`).
- [ ] **Step 4: Capability-panel honesty.** In `agent/page.tsx:39-46`, distinguish fetch failure ("Couldn't reach the server — Retry") from genuinely empty (keep current copy). Do not hide the Status grid silently on failure — show `ErrorState` in its place.
- [ ] **Step 5: Verify** typecheck + build + contract test green. **Step 6: Commit** (one commit per page-group is fine).

---

### Task B4b: Chat response formatting (markdown)

**Files:** Modify `frontend/src/app/(app)/chat/page.tsx`; `frontend/package.json` (one justified dependency exception — see below).

- [ ] **Step 1: Install renderer.** `npm install react-markdown remark-gfm` in `frontend/`. Justification for breaking the no-new-deps rule: assistant output is untrusted model text; hand-rolled markdown is an XSS risk, a real parser is the safe option. Record installed versions. No `rehype-raw` — raw HTML must never render.
- [ ] **Step 2: Render assistant messages as markdown.** Wrap assistant bubbles in `<ReactMarkdown remarkPlugins={[remarkGfm]}>` with a `components` map for `a` (target blank, underline), `code`/`pre` (existing mono styling + horizontal scroll), `ul`/`ol` (list styling — Tailwind preflight strips bullets, so re-add explicitly). User messages stay plain text. Streaming is not used (chat waits for the full reply), so no incremental-render work is needed.
- [ ] **Step 3: Verify.** Typecheck + build clean; manual: ask for "a short list with a code block" and assert headers/list/code render (no visible `***` or backticks); send a message containing `<img src=x onerror=alert(1)>` and assert it renders as inert text (no execution). **Step 4: Commit.**

---

### Task B5: "Ask @person" — the missing core flow

**Files:** Modify `frontend/src/app/(app)/chat/page.tsx`, `frontend/src/lib/api/tasks.ts` (+ `tasks/page.tsx:328-422` delegate modal), `frontend/src/lib/api/approvals.ts` (outbox source), `frontend/src/app/(app)/inbox/page.tsx`.

- [ ] **Step 1: Capability picker data.** Replace the hardcoded 4-item capability dropdown (`tasks/page.tsx:365-374`) with options from the live `getCapabilities()` registry (already exists in `identity.ts`). The delegate modal keeps its JSON advanced field but gains capability + purpose + contact pickers so a non-developer can complete it.
- [ ] **Step 2: Chat entry point.** Add an "Ask a person" affordance in Chat opening the same picker (contact → capability → purpose → summary), creating the task via the existing delegate endpoint and linking to the Inbox outbox. No free-text-to-action guessing: explicit selection only.
- [ ] **Step 3: Inbox outbox + done-today.** Add "Waiting on others" (tasks where the user is sender, by status) and "Done today" (recently decided items; keep them visible with their outcome instead of vanishing after 600 ms — change `inbox/page.tsx:67-69` to move decided cards to the Done pane).
- [ ] **Step 4: Verify** typecheck + build + contract test; manual click-through of ask → outbox → approve → done. **Step 5: Commit.**

---

### Task B6: People — connect from search + person detail + invite

**Files:** Modify `frontend/src/app/(app)/people/page.tsx`, `frontend/src/lib/api/a2a.ts` (+ `people.ts`); create `frontend/src/app/(app)/people/[id]/page.tsx`; modify `frontend/src/app/(app)/agent/page.tsx`.

- [ ] **Step 1: Connect-from-search.** Directory results gain a Connect button (enabled only when `verified === true`; otherwise show the unverified reason, already returned). Reuse the existing connect-by-card flow underneath (`people/page.tsx:59-76`) — no new backend route.
- [ ] **Step 2: Person detail page.** New `people/[id]/page.tsx` reachable from both lists: display name + `@handle`, fingerprint with copy, pinned-key status, capability list (from the card/discovery payload already available — read what the search endpoint returns first and render exactly those fields), last exchange (from tasks by peer), link to per-person policy (permissions page anchor). Add the hub/static-link entries so the M10 reachability tests cover it.
- [ ] **Step 3: Invite path + account.** On the Agent page, add card URL + copy button (backend `getAgentCard` wrapper exists and is dead — wire it up) with a note that directory listing is opt-in. No QR library (YAGNI). Add a change-password form (the `changePassword` wrapper in `auth.ts:60-71` is dead — wire it up here or on the login page; read both first). No QR library (YAGNI).
- [ ] **Step 4: Verify** typecheck + build + contract test (static-link and reachability tests must see the new page). **Step 5: Commit.**

---

### Task B7: Harden the M10 contract tests for the gaps found

**Files:** Modify `tests/test_m10_frontend_contract.py`.

- [ ] **Step 1: Destructured reads.** Extend the read-check to catch `const {field} = await fn()` + later `field` use for the three fan-out pages (Inbox, AppShell badge, Chat), so a rename of `ApprovalsResult.items` breaks CI instead of three pages.
- [ ] **Step 2: Envelope strictness.** For the four approval sources, assert the documented key (`tasks`/`workflows`/`runs`) is present AND non-trivially shaped (e.g. first item carries the fields the page reads) — the current key-presence check passes while the page silently takes a fallback path.
- [ ] **Step 3: Deny-body verification.** Assert each deny/approve body the Inbox sends (`approvals.ts:216-249`) validates against its backend request schema (workflow cancel vs autonomy/orchestration reject, `{reason}` vs `{notes}`) — read each route schema first, do not guess.
- [ ] **Step 4: Re-run** the contract suite — green. **Step 5: Commit.**

---

## Phase B acceptance gate

- `npx tsc --noEmit` clean, `npm run build` clean, M10 suite green.
- Manual script (backend + frontend running): register → fingerprint → directory search → connect → ask → outbox shows it → approve from Inbox → done-today shows outcome → audit shows it. Expire the session mid-way → lands on `/login`. Stop the backend → every page shows an error banner with Retry (none shows a false empty state).
