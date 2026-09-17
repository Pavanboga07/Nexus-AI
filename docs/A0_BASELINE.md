# Pre-fix baseline (Task A0) — rollback pointer

This file is the durable copy of the A0 baseline record. The original lives at
`C:\Users\91862\AppData\Local\Temp\opencode\neon-baseline-2026-09-17\README.md`
(OS temp — do not rely on it).

- **Baseline commit:** `88c88ea20675e61d6d41657a9693e3e7c4403639`
  (`wip: pre-fix baseline (M0-M11 + capabilities endpoint)`)
- **Tag:** `a0-baseline` (rollback point for the whole fix program).
- **Branch/project:** `main` in `D:\AI nexus\nexus`.
- **Notable paths buried in the baseline commit** (touched again by Phase A/B):
  `app/a2a/capabilities.py`, `app/api/routes/identity.py`
  (GET /identity/capabilities), `app/api/routes/system.py`,
  `frontend/src/app/(app)/agent/page.tsx`, `frontend/src/lib/api/identity.ts`.
- **Neon snapshot:** NOT taken — `pg_dump`/`pg_restore` are not installed on
  this machine. Zero Neon writes were made by A0. Before any task that writes
  to the shared Neon dev DB, take the dump with this recipe (password via
  `PGPASSWORD` env only, never on the command line):
  1. Read `DATABASE_URL` from `D:\AI nexus\nexus\.env`.
  2. Convert scheme `postgresql+asyncpg` → `postgresql` and query
     `ssl=require` → `sslmode=require`.
  3. `$env:PGPASSWORD="<from .env>"; pg_dump -Fc "<converted-url>" -f neon-baseline.dump`
  4. Verify: `pg_restore --list neon-baseline.dump | Select-Object -First 5`
- **Test baseline (2026-09-17, local PG absent, `NEXUS_ALLOW_NO_DB=1`):**
  `tests/test_workflows.py` 16 skipped; `tests/test_part10_autonomy.py`
  1 passed, 14 skipped; `tests/test_m0_security_regressions.py` 7 passed,
  5 skipped. Total: 8 passed, 35 skipped, 0 failed.
