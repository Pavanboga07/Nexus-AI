# Phase C — Ops & Hardening Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the safety nets automatic: CI enforces the suites, builds reproduce, tests fail (not skip) without a DB, backups actually restore, and the gateway is orchestrator-ready.

**Architecture:** Smallest change that closes each gap. No new infrastructure (still Postgres-only per decision D6, still Render free tier for the gateway). Docs are updated where behavior changes.

**Tech Stack:** GitHub Actions, `pip-compile` (pip-tools), pytest + pytest-cov, `pg_dump/pg_restore`, Render.

---

## Ground rules

- Secrets never touch git, CI logs, or scripts (read from env at runtime).
- Every task ends with the affected suite green + a commit.
- Order matters: C1 (CI) first so later tasks are enforced from birth.

## File map

| File | Responsibility |
|---|---|
| `.github/workflows/ci.yml` (new, in `nexus/` repo scope — see C1) | Migrate → test → conformance → build on every push |
| `requirements.lock.txt` (new, both repos) | The transitive closure `requirements.txt:10-12` already promises |
| `nexus-gateway/app/main.py:427` + `app/config.py` | Gateway `/readyz` |
| `nexus/tests/conftest.py:137-139` | Fail (not skip) without Postgres, matching the gateway |
| `pytest.ini` / requirements | Coverage gate on `app/a2a,app/policy,app/identity` |
| `nexus/docs/DR_RUNBOOK.md`, `nexus/scripts/` | Scheduled backups + rehearsed restore with evidence |
| `nexus-gateway/tests/` | Real socket-level delivery + handle-conflict tests |
| `nexus-sdk/tests/` (new) | In-package SDK tests + pinned CI matrix |

Note: `nexus/` and `nexus-gateway/` are separate git repos. CI (C1) means one workflow file per repo (backend workflow in the nexus repo covering app+frontend+SDK paths if they share it — check repo roots first; the gateway repo gets its own smaller workflow).

---

### Task C1: CI that enforces everything

- [ ] **Step 1: Check repo roots.** Confirm whether `D:\AI nexus\nexus`, `nexus-gateway`, `nexus-sdk` share one git repo or are separate (gateway has its own remote; verify the others with `git remote -v`). One workflow file per repo root.
- [ ] **Step 2: Backend workflow.** Create `.github/workflows/ci.yml`: `docker compose up -d db` (postgres:16 + pgvector image matching `docker-compose.yml`), `pip install -r requirements.txt`, `alembic upgrade head`, `pytest -q` (full suite, no allow-skip flag — DB tests must run), then the conformance VECTOR check (`pytest tests/test_m9_conformance.py`), then `npm --prefix frontend install && npm --prefix frontend run build`. Fail the job on any red step.
- [ ] **Step 3: Gateway workflow.** Same shape, smaller: pg service, `pip install`, `alembic upgrade head` (check the gateway's alembic setup first), `pytest -q`.
- [ ] **Step 4: Verify** by pushing to a branch and watching it run (or `act` locally if available). **Step 5: Commit.**

---

### Task C2: Lockfiles (close the false promise)

- [ ] **Step 1: Generate.** `pip install pip-tools`, then `pip-compile --generate-hashes requirements.in` — but no `.in` file exists, so first split: create `requirements.in` from the direct deps currently pinned with `==` in `requirements.txt:14-49` (same for the gateway, `:12-37`), then compile to `requirements.lock.txt`. Keep `requirements.txt` as the constrained install file worn by deploys, or replace it — decide once, document in the file header, do both repos identically.
- [ ] **Step 2: Verify.** Fresh venv → install from lockfile → `pytest tests/test_m11_hardening_regressions.py` (the suite that self-checks pins) green.
- [ ] **Step 3: Commit** lockfiles + `.in` files.

---

### Task C3: Gateway `/readyz`

- [ ] **Step 1: Failing test.** `GET /readyz` returns 200 with dependency checks when the DB is up, 503 when it is down (mirror the backend contract in `nexus/app/api/readiness.py:152-170`; gateway DB down = not ready; no gateway concept applies — it IS the gateway).
- [ ] **Step 2: Implement** in `nexus-gateway/app/main.py` next to `/health` (`:427-437`), reusing the existing engine/session. Point Render's health check at it only after it proves stable (leave `render.yaml:9` on `/health` until then — note why in a comment).
- [ ] **Step 3: Re-run** gateway suite — green. **Commit.**

---

### Task C4: Main-app tests fail without Postgres (no more silent green)

- [ ] **Step 1: Change `nexus/tests/conftest.py:137-139`** to `pytest.fail(...)` with the same clear message, unless `NEXUS_ALLOW_NO_DB=1` (exact pattern already in `nexus-gateway/tests/conftest.py:61,171-172` — copy it).
- [ ] **Step 2: Run twice.** With local PG up: full suite green. With PG down and the flag unset: suite fails fast with the message. With `NEXUS_ALLOW_NO_DB=1`: skips as before.
- [ ] **Step 3: Commit.** (From here on, CI in C1 guarantees the DB-backed path runs.)

---

### Task C5: Coverage gate on the security core

- [ ] **Step 1: Measure.** Add `pytest-cov` to requirements, run `pytest --cov=app/a2a --cov=app/policy --cov=app/identity --cov-report=term-missing -q`, record the numbers.
- [ ] **Step 2: Gate.** If ≥80% already (likely — these are the best-tested packages), set `fail-under=80` for exactly those three packages in `pytest.ini` (report-only elsewhere, per PLAN §6). If below, write the missing tests first (no gate-lowering without a written reason in the commit message).
- [ ] **Step 3: Commit.**

---

### Task C6: Backups that have actually restored once

- [ ] **Step 1: Script the dump.** Add `nexus/scripts/backup.py`: `pg_dump -Fc` from `DATABASE_URL` (env at runtime) to a timestamped file + `pg_restore --list` verify + stdout summary. No credentials in code, args, or logs.
- [ ] **Step 2: Rehearse.** Dump Neon (small, dev data), restore into local docker `nexus_restore` DB, run `alembic current` + `/readyz`-equivalent checks against it, record the transcript.
- [ ] **Step 3: Update `docs/DR_RUNBOOK.md`.** Replace "has not been rehearsed" with the dated transcript, set the schedule (daily dump + where it runs), and keep the `NEXUS_IDENTITY_KEY`-loss warning (irrecoverable by design — the mitigation is key backup, say where).
- [ ] **Step 4: Commit.**

---

### Task C7: Prove cross-process delivery + handle conflicts over real sockets

- [ ] **Step 1: Delivery test.** In `nexus-gateway/tests/`, start two gateway app instances (separate `TestClient`/ports, shared DB via the existing `db_engine` fixture pattern): agent A on instance 1 sends to agent B on instance 2 over real WebSockets; assert exactly-once delivery; kill instance 2 mid-flight (close the socket unacked), reconnect, assert redelivery then ack-driven settle. Mark it slow-tolerant (generous timeouts — CI runners are slow).
- [ ] **Step 2: Handle-conflict test.** Open a real WebSocket with a conflicting handle; assert the `4003` close from `app/main.py:719-732` (the repo-level arbitration is tested; the socket path is not).
- [ ] **Step 3: Run** the gateway suite — green. **Commit.**

---

### Task C8: SDK self-tests + honest pins

- [ ] **Step 1: Tests.** Create `nexus-sdk/tests/test_roundtrip.py`: canonical-vector agreement with `nexus/tests/vectors/conformance.json`, sign/verify both directions, tamper rejection. Run with plain `pytest` from the SDK dir in CI (C1 gateway/backend workflows gain an SDK job, or the SDK gets its own workflow if it is its own repo — decided in C1 Step 1).
- [ ] **Step 2: Pins.** Narrow `cryptography>=42.0, httpx>=0.27.0` only if CI proves a newer major breaks; otherwise keep ranges but record the CI-tested versions in the SDK README. No change for change's sake.
- [ ] **Step 3: Commit.**

---

## Phase C acceptance gate

- CI green on both repos (migrate → test → conformance → frontend build).
- `requirements.lock.txt` installs clean in a fresh venv.
- PG-down run fails fast (both repos); PG-up run fully green.
- DR runbook carries a dated, pasted restore transcript.
- Gateway suite includes the socket-level delivery + conflict tests.
