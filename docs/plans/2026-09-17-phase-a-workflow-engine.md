# Phase A — Workflow Engine Correctness Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the workflow engine do what it claims: autonomy can drive it, remote steps resume with policy enforced, approvals can't replay history, crashes recover, advancement survives restarts.

**Architecture:** Fix the engine in place (no rewrite). Each task is test-first, keeps the suite green, and ends in a commit. Jobs reuse the existing `JobWorker` + `workflow.advance` handler already registered in `app/main.py:543`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL (Neon for app, local docker for tests), pytest.

---

## Ground rules (every task)

- Prerequisite for any test run: `docker compose up -d db` in `D:\AI nexus\nexus`, then `python -m pytest <paths> -q`.
- Red-green: write the failing test, run it, implement, re-run the affected files PLUS `tests/test_workflows.py tests/test_part10_autonomy.py tests/test_m10_frontend_contract.py`.
- Never break the M10 contract: no response-shape renames; new fields are additive only.
- `failure_reason` columns are `String(255)` — always truncate to 250 chars (sites centralized in Task A6).

## File map

| File | Responsibility after this phase |
|---|---|
| `app/autonomy/executor.py:249-282` | CREATE_WORKFLOW uses a registered step type + `start_workflow()` |
| `app/autonomy/service.py:289-293,340-355` | Correct `owner_id`-first calls into `WorkflowService` |
| `app/workflows/service.py` | Approval guard, policy-checked resume, expiry-on-touch, truncation, run linkage |
| `app/workflows/repository.py` | Recovery query covers non-RUNNING states; lookup runs by `workflow_id` (new method if missing) |
| `app/workflows/handlers.py:199-235` | Code-mapped transient errors; canonical context reads |
| `app/main.py` (~line 426-445, ~543) | Register workflow resume callback on the A2A bus; enqueue `workflow.advance` |
| `app/orchestration/orchestrator.py:391-405` | No consent minted for workflow-backed approvals |
| `tests/test_workflows.py`, `tests/test_part10_autonomy.py` | Regression coverage for every fix; async drain helper for job-based advancement |

---

### Task A0: Safe baseline

**Files:** none (git + DB only).

- [ ] **Step 1: Commit current work.** `git add -A; git commit -m "wip: pre-fix baseline (M0-M11 + capabilities endpoint)"` in `D:\AI nexus\nexus`. Verify `git status --short` shows only intended leftovers.
- [ ] **Step 2: Snapshot Neon.** `pg_dump -Fc` the Neon DB to a local file (connection string from `.env` `DATABASE_URL`, password via env, never on the command line). Record the file path.
- [ ] **Step 3: Green baseline.** `docker compose up -d db`, then `$env:NEXUS_ALLOW_NO_DB="1"; python -m pytest tests/test_workflows.py tests/test_part10_autonomy.py tests/test_m0_security_regressions.py -q`. Record pass/skip counts. If anything fails, stop — the baseline must be green before fixes.

---

### Task A1: Fix autonomy→workflow call arity

**Files:** Modify `app/autonomy/service.py:291,342`. Test: `tests/test_part10_autonomy.py` (append).

- [ ] **Step 1: Write the failing test.** A run with `workflow_id` set is cancelled; assert the linked workflow reaches `CANCELLED` (today it stays put because `cancel_workflow(run.workflow_id)` puts the UUID in `owner_id` and raises `TypeError` into the `except`). A second test: `reconcile_on_startup` with a workflow-backed run in a terminal workflow state asserts the run leaves `RUNNING` (today `get_workflow(r.workflow_id)` always throws → `STOPPED`).
- [ ] **Step 2: Run to verify both fail.**
- [ ] **Step 3: Implement.** Confirm the run model carries `owner_id` (read `app/autonomy/models.py`), then:

```python
await self._workflows.cancel_workflow(run.owner_id, run.workflow_id)
# ...
wf = await self._workflows.get_workflow(r.owner_id, r.workflow_id)
```

- [ ] **Step 4: Re-run.** `pytest tests/test_part10_autonomy.py tests/test_workflows.py -q` — green.
- [ ] **Step 5: Commit.**

---

### Task A2: Fix CREATE_WORKFLOW (registered type + real start)

**Files:** Modify `app/autonomy/executor.py:249-282`. Test: `tests/test_part10_autonomy.py` (append).

- [ ] **Step 1: Write the failing test.** Execute a `CREATE_WORKFLOW` action with the executor wired to a real `WorkflowService`; assert the created workflow is NOT stuck `PENDING` afterwards (today: `step_number` is silently dropped by `WorkflowStepSpec`, `generic_action` is unregistered, and `advance_workflow()` on `PENDING` returns immediately per `service.py:265-266`).
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement.** Map the action payload to a registered step type (fail closed when unmappable) and start properly:

```python
elif act_norm == ActionType.CREATE_WORKFLOW.value:
    wf_type = action.payload.get("workflow_type", "autonomous_workflow")
    payload = action.payload or {}
    if payload.get("target_agent_id") or payload.get("recipient_agent_id"):
        step_type: str = "a2a_task"
    elif payload.get("tool_name"):
        step_type = "tool_execution"
    else:
        return ExecutionResult(
            status=RunStatus.FAILED,
            step_output={"error": "CREATE_WORKFLOW payload maps to no registered step type"},
        )
    steps_spec = [WorkflowStepSpec(step_type=step_type, input_payload=payload)]
    if self._workflow_service:
        wf = await self._workflow_service.create_workflow(
            owner_id, workflow_type=wf_type, purpose=action.purpose, steps=steps_spec,
        )
        run.workflow_id = wf.workflow_id
        wf_run = await self._workflow_service.start_workflow(owner_id, wf.workflow_id)
        ...
```

Keep the existing `waiting_approval` propagation, and add the missing `waiting_remote` propagation (`executor.py:277-282` drops it — compare `wf_run.status == "waiting_remote"` too and set `RunStatus.WAITING_REMOTE`).

- [ ] **Step 4: Re-run** autonomy + workflow suites — green.
- [ ] **Step 5: Commit.**

---

### Task A3: Approval accepts only the WAITING step

**Files:** Modify `app/workflows/service.py:555-569`. Test: `tests/test_workflows.py` (append).

- [ ] **Step 1: Write the failing test.** Drive a workflow to `WAITING_APPROVAL`, approve once (step completes), then call `approve_workflow` again with the now-`COMPLETED` step's `step_id`; assert `WorkflowConflictError` (today it resets the step to `PENDING` and re-runs its side effects).
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** (insert after the `target_step.workflow_id != workflow_id` check at line 559):

```python
if target_step.status != StepStatus.WAITING.value:
    raise WorkflowConflictError(
        f"Step {step_id} is not awaiting approval (current status: {target_step.status})"
    )
```

- [ ] **Step 4: Re-run** — green. **Step 5: Commit.**

---

### Task A4: Resume path — wire the callback and enforce policy

**Files:** Modify `app/main.py` (lifespan, after `workflow_service` is built ~line 441), `app/workflows/service.py:654-693`. Test: `tests/test_workflows.py` (append).

- [ ] **Step 1: Write failing tests.** (a) With a real `A2AService`, register `workflow_service.handle_task_completion` via `register_task_completion_callback`, park a step in `WAITING_REMOTE`, deliver the completion through the bus; assert the workflow resumes (today nothing is registered, so it never resumes). (b) Same setup, but the step's policy evaluates DENY on resume; assert the payload is NOT ingested and the step does not complete.
- [ ] **Step 2: Run to verify both fail.**
- [ ] **Step 3: Implement wiring** in `app/main.py` next to the other service wiring:

```python
if a2a_service is not None and workflow_service is not None:
    a2a_service.register_task_completion_callback(
        workflow_service.handle_task_completion
    )
```

(The callback signature `(task_id, payload)` matches `handle_task_completion(task_id, response_payload)`.)

- [ ] **Step 4: Implement policy on resume.** In `handle_task_completion`, after loading `step`/`wf` and before writing anything, re-run the step's own evaluation (same call the advance loop uses):

```python
handler = self._registry.get(step.step_type)
if handler is not None:
    resume_ctx = WorkflowStepContext(
        owner_id=wf.owner_id, workflow_id=step.workflow_id,
        step_id=step.step_id, step_number=step.step_number,
        purpose=wf.purpose, workflow_context=dict(wf.context_data or {}),
        memory_manager=self._memory_manager, policy_service=self._policy,
        tool_service=self._tool_service, a2a_service=self._a2a_service,
        identity_service=self._identity_service,
    )
    resume_eval = await self._policy.evaluate(
        wf.owner_id, handler.get_evaluation_request(resume_ctx, step.input_payload or {})
    )
    if resume_eval.decision is PolicyDecision.DENY:
        # do NOT ingest the payload; fail closed (truncate: A6 helper)
        ...
    if resume_eval.decision is PolicyDecision.ASK:
        # park back in WAITING_APPROVAL without ingesting
        ...
```

`WorkflowStepContext` construction mirrors `service.py:335-347` exactly. DENY → `_fail_step_and_workflow` (truncated reason); ASK → step `WAITING` + workflow `WAITING_APPROVAL`, commit, audit, return.

- [ ] **Step 5: Re-run** workflow + M0 suites — green. **Step 6: Commit.**

---

### Task A5: Advance via jobs, not inline in requests

**Files:** Modify `app/workflows/service.py` (`start_workflow:233`, `approve_workflow:607`, `handle_task_completion:692`), `app/main.py:539-543` (check the `_advance_workflow_job` handler signature first and match it). Tests: `tests/test_workflows.py` (add drain helper, update callers).

- [ ] **Step 1: Read the job API.** Read `app/jobs/` enqueue function and the `workflow.advance` handler at `main.py:539-543`. Record the exact enqueue call and payload shape — every later step uses it verbatim.
- [ ] **Step 2: Add a test drain helper** in `tests/test_workflows.py`:

```python
async def drain_workflow_jobs(job_service_or_worker):
    """Run pending workflow.advance jobs inline until the queue is empty."""
```

Implement against the real enqueue API from Step 1 (no mocks of the service itself).

- [ ] **Step 3: Convert ONE call site** (`start_workflow:233`): enqueue `workflow.advance` with the workflow id, then return a fresh read (`self._get_wf(workflow_id)`) instead of `await self.advance_workflow(...)`. Update the tests that asserted synchronous completion to `drain_workflow_jobs()` first. Run `tests/test_workflows.py` — green before proceeding.
- [ ] **Step 4: Convert the other two sites** (`approve_workflow:607`, `handle_task_completion:692`) the same way. Re-run workflow + autonomy + M10 suites — green.
- [ ] **Step 5: Kill-mid-workflow proof.** New test: start a workflow, stop the worker mid-`RUNNING` (simulate by abandoning the drain), run `recover_interrupted_workflows()`, drain, assert completion. This is the acceptance test for the whole task.
- [ ] **Step 6: Commit.**

---

### Task A6: Recovery scope, expiry-on-touch, truncation, cleanup sets

**Files:** Modify `app/workflows/repository.py` (`find_interrupted_workflows`), `app/workflows/service.py` (recovery, `get_workflow`, `list_workflows`, `_fail_step_and_workflow`, cancel/expiry blocks).

- [ ] **Step 1: Failing tests.** (a) A `WAITING_APPROVAL` workflow abandoned before a restart is returned by recovery (today the query is `RUNNING`-only). (b) An expired `WAITING_APPROVAL` workflow surfaces as `EXPIRED` on `get_workflow` without any advance running (today it sits non-terminal forever). (c) A 500-char reason passed to `_fail_step_and_workflow` stores ≤250 chars (today the commit itself can fail on Postgres).
- [ ] **Step 2: Run to verify all three fail.**
- [ ] **Step 3: Implement.** Widen `find_interrupted_workflows` to `RUNNING/WAITING_APPROVAL/WAITING_REMOTE/PENDING`; keep per-state handling (RUNNING steps retry-or-fail as today; WAITING rows re-drive via `advance_workflow`, which already expiry-checks at `service.py:244-263`). Enforce sequentiality: after acquiring the next pending step and before marking it `RUNNING`, re-check that no other step of the workflow is `RUNNING` — if one is, roll back and return (another advancer owns it; concurrent advancers must never execute sequential steps in parallel). Add an expiry check at the top of `get_workflow`/`list_workflows`/`approve_workflow` (same predicate as lines 244-249; factor it into a `_expire_if_overdue(wf)` helper used by all four sites). Truncate once, centrally, in `_fail_step_and_workflow`: `reason[:250]`. Unify the three cleanup skip-sets (`service.py:252-254, 634-637, 709-711`): non-terminal steps (`PENDING/WAITING`) → `SKIPPED`; `RUNNING` under a terminal parent → `FAILED` with reason `"parent {cancelled|expired} while step running"`; and guard handler-completion writes to no-op when the parent is already terminal (check at write time in the completion blocks).
- [ ] **Step 4: Re-run** — green. **Step 5: Commit.**

---

### Task A7: Context keys and retry mapping (no silent behavior change)

**Files:** Modify `app/workflows/service.py` (add `get_step_output` helper), `app/workflows/handlers.py:199-235`.

- [ ] **Step 1: Failing tests.** (a) Two steps of the same type: assert both outputs retrievable by step number (today the second overwrites `ctx[step_type]`). (b) `A2ATaskStepHandler` with a policy-denied send raises the coded error and is classified non-transient (today substring matching decides).
- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement.** Keep writing both `ctx[f"step_{n}"]` and `ctx[step_type]` (back-compat), add `get_step_output(ctx, step_number)` preferring the numbered key, and switch the handler's dead lookups (`handlers.py:199-208`) to it. Replace substring transient detection with `A2AError` code mapping (`app/a2a/errors.py`): transport/timeout codes → transient; policy/deny/validation codes → non-transient; uncaught `Exception` → non-transient `FAILED` (programming errors must not retry 3×). Update any test that asserted retries on generic exceptions. Delete dead code found in the audit (verify zero references first): the `_slugify` import in `service.py:36` (use the module-level helper the service already defines, or move one copy), the never-raised `WorkflowAccessDeniedError` imports, and the unused `DisclosureScope` import in `handlers.py:20`.
- [ ] **Step 4: Re-run** — green. **Step 5: Commit.**

---

### Task A8: Single consent per approval + run linkage

**Files:** Modify `app/orchestration/orchestrator.py:391-405`, `app/workflows/service.py` (completion branch `service.py:288-299`), `app/workflows/repository.py` or autonomy repo (lookup runs by `workflow_id` — add the method if missing).

- [ ] **Step 1: Failing tests.** (a) Approve a workflow-backed orchestration run; assert no unconsumed single-use `ALLOW` consent remains for that owner afterward (today the orchestrator-minted one lingers). (b) Complete a workflow linked from an autonomy run; assert the run leaves its waiting state (today nothing maps completion back).
- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement.** In `approve_run`, skip the consent mint when `run.workflow_id` is set (the workflow's own approval mints the exact-scope consent at `service.py:581-589`); keep it only for the direct plan-execution path. In the workflow completion branch, look up linked autonomy runs by `workflow_id` and sync terminal/waiting states (`COMPLETED`→completed, `FAILED/CANCELLED`→failed with reason, `WAITING_APPROVAL`→waiting-approval).
- [ ] **Step 4: Re-run** autonomy + workflow + orchestration suites — green. **Step 5: Commit.**

---

## Phase A acceptance gate (run all before touching Phase B)

```bash
docker compose up -d db
python -m pytest tests/test_workflows.py tests/test_part10_autonomy.py tests/test_part12_orchestration.py tests/test_m0_security_regressions.py tests/test_m5_consistency_regressions.py tests/test_m7_jobs_regressions.py tests/test_m10_frontend_contract.py -q
```

Green (no new skips vs the A0 baseline) or stop. Then proceed to `docs/plans/2026-09-17-phase-b-frontend-product.md`.
