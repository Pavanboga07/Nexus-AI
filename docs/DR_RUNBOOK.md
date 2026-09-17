# Backup & Disaster Recovery Runbook

**Scope:** the two PostgreSQL databases this system owns, plus the one secret that
cannot be regenerated.

**This document is a plan, not a proof.** Nothing in this repository validates it,
and it has not been rehearsed against a real deployment. The verification steps
are written so that a rehearsal produces evidence rather than confidence.

---

## 1. What actually needs to survive

| Asset | Where | Recoverable by | Cost of loss |
|---|---|---|---|
| Application database | Postgres, `DATABASE_URL` (Docker `nexus_postgres`, port 5433) | Backup | Total: owners, agents, memories, policies, consents, tasks, workflows, autonomy runs, jobs |
| Gateway database | Postgres, `GATEWAY_DATABASE_URL` | Backup | Registered agent directory + offline queue: undelivered A2A messages |
| **`NEXUS_IDENTITY_KEY`** | Deployment secret | **Nothing. Irrecoverable.** | Every stored agent private key becomes undecryptable. See §5 |
| `NEXUS_SESSION_KEY` | Deployment secret | Regenerate (logs everyone out) | Low |
| LLM API key | Deployment secret | Reissue from the provider | Low |
| Gateway registration secret | Deployment secret | Regenerate (peers re-register) | Medium |
| Docker volume (`pgdata`) | Host filesystem | Host backup | Same as the database row |

Two properties of this system shape the whole plan:

1. **The vector index is derived data.** `ix_memories_embedding_hnsw` is built by
   `scripts/build_vector_index.py`, and the `memories.embedding` column's
   dimension is pinned to whatever embedder produced it. A restore that changes
   `NEXUS_EMBEDDING_PROVIDER` or `NEXUS_EMBEDDING_DIMENSIONS` leaves embeddings
   that cannot be searched consistently. **Restore the config with the data.**
2. **Identity is not data.** Agent rows and keys live in the database, but they
   are only *usable* with the matching `NEXUS_IDENTITY_KEY`. A database backup
   without that secret is a backup of unreadable ciphertext.

---

## 2. Backup

### 2.1 Application database

```bash
# Custom format: compressed, and restorable table-by-table.
docker exec nexus_postgres pg_dump -U nexus -Fc -d nexus > nexus-$(date +%Y%m%dT%H%M%S).dump

# Verify the dump is readable BEFORE trusting it. An unverified backup is a
# guess, and this is the step that turns it into a backup.
pg_restore --list nexus-*.dump > /dev/null && echo "dump is readable"
```

### 2.2 Gateway database

```bash
docker exec nexus_gateway_postgres pg_dump -U nexus -Fc -d nexus_gateway \
  > nexus-gateway-$(date +%Y%m%dT%H%M%S).dump
```

### 2.3 Secrets

Back these up **out of band** - a secrets manager, or a sealed envelope. Never
next to the dump: a backup that contains both the ciphertext and the key is a
plaintext backup.

```
NEXUS_IDENTITY_KEY          # irreplaceable - see §5
NEXUS_SESSION_KEY
NEXUS_GATEWAY_SECRET
NEXUS_LLM_API_KEY
```

### 2.4 Schedule

| Tier | Frequency | Retention | Why |
|---|---|---|---|
| Full dump | Daily | 30 days | Point-in-time recovery to yesterday is the common case |
| Secrets | On change | Forever | Rotating a secret is the moment you must record it |
| Pre-migration dump | Before every `alembic upgrade` | Until the release is confirmed | An expand-contract migration is reversible; a bad one is not |

**Pre-migration dumps are not optional.** Alembic can downgrade, but only if the
downgrade path was written and tested - and for a destructive migration it often
cannot be, which is exactly why PLAN.md §7 requires expand-contract.

---

## 3. Restore

### 3.1 Application database

```bash
# 1. Stop writers FIRST. Restoring under a live app produces a database that is
#    half old and half new, with no error to tell you.
docker stop nexus-api

# 2. Create a fresh database. Never restore over one in place: if the restore
#    fails halfway you have destroyed the only copy you had.
docker exec nexus_postgres createdb -U nexus nexus_restore
docker exec -i nexus_postgres pg_restore -U nexus -d nexus_restore --clean --if-exists \
  < nexus-20260101T030000.dump

# 3. Verify BEFORE swapping.
docker exec nexus_postgres psql -U nexus -d nexus_restore -c "\dt" | head -30
docker exec nexus_postgres psql -U nexus -d nexus_restore \
  -c "SELECT count(*) FROM owners; SELECT count(*) FROM agents; SELECT count(*) FROM memories;"

# 4. Confirm the schema is at the expected revision, not merely present.
DATABASE_URL=postgresql+asyncpg://nexus:nexus@localhost:5433/nexus_restore \
  python -m alembic current          # must print the revision in the backup

# 5. Swap.
docker exec nexus_postgres psql -U nexus -c "ALTER DATABASE nexus RENAME TO nexus_old;"
docker exec nexus_postgres psql -U nexus -c "ALTER DATABASE nexus_restore RENAME TO nexus;"

# 6. Start and VERIFY READINESS, not just liveness.
docker start nexus-api
curl -fsS http://127.0.0.1:8000/readyz | jq .
#    `status: ready` means database + identity + jobs, and identity means the
#    key integrity check ran. A 200 from /health would prove nothing here.
```

### 3.2 Vector index

A restore brings back the rows and the index DDL. Whether the index is *valid*
is a separate question, because `pg_restore` can land data after index creation.

```bash
docker exec nexus_postgres psql -U nexus -d nexus -c \
  "SELECT indexrelid::regclass AS index, indisvalid FROM pg_index
   WHERE indexrelid::regclass::text LIKE '%hnsw%';"
# indisvalid must be true. If false:
python scripts/build_vector_index.py --database-url "$DATABASE_URL" --yes
```

### 3.3 Gateway database, and the queue

```bash
docker stop nexus-gateway
docker exec nexus_gateway_postgres createdb -U nexus nexus_gateway_restore
docker exec -i nexus_gateway_postgres pg_restore -U nexus -d nexus_gateway_restore \
  --clean --if-exists < nexus-gateway-20260101T030000.dump
# swap as in §3.1, then:
docker start nexus-gateway
```

**Expected data loss:** every message that was queued for an agent that was
offline at the moment of the last backup. This is bounded by the backup
interval, and it is the only asset here with an unavoidable gap.

After restoring, the redelivery sweep (every `GATEWAY_REDELIVERY_INTERVAL_SECONDS`)
flushes queue rows whose recipients reconnect. Confirm with:

```bash
docker exec nexus_gateway_postgres psql -U nexus -d nexus_gateway \
  -c "SELECT state, count(*) FROM gateway_messages GROUP BY state;"
```

---

## 4. Recovery objectives

These are **targets to be agreed with whoever owns the product**, not measured
achievements. Stated so a rehearsal can be scored against something.

| Scenario | RPO (data loss) | RTO (downtime) |
|---|---|---|
| Single API replica dies | 0 | 0 (other replicas serve; `/readyz` drains the dead one) |
| Application database corrupted | ≤ 24h (last dump) | ~30 min |
| Gateway database lost | ≤ 24h of queued messages | ~15 min |
| Host lost, secrets backed up | ≤ 24h | ~2h (provision + restore + verify) |
| `NEXUS_IDENTITY_KEY` lost | **Everything A2A** | See §5 |

The first row is the reason `/readyz` exists and is separate from `/health`:
a replica that cannot reach the database must be **drained**, not restarted,
because restarting it makes a database incident into a crash loop.

---

## 5. When `NEXUS_IDENTITY_KEY` is lost

This is the one scenario with no clean recovery, and the failure mode is worth
understanding before it happens.

Every agent's Ed25519 private key is stored AES-256-GCM encrypted under a key
derived from `NEXUS_IDENTITY_KEY`. Lose the secret and the private keys are
undecryptable ciphertext. `IdentityService.verify_agent_key_integrity` detects
exactly this at startup and **refuses to boot** (`IdentityCorruptionError`) -
which is correct, and should not be worked around by disabling the check: an
identity that can be silently replaced is one that can be silently taken over.

What still works afterwards:
- Owners, sessions, memories, policies, consents, tasks, workflows (all intact)
- The API, the UI, chat

What is gone:
- Every agent_id. **Agent ids are the SHA-256 fingerprint of the public key**, so
  new key material means new ids - not a rename of the old ones.
- Every trust relationship: peers pinned the old fingerprint, in their own
  databases, out of reach.

Recovery:

1. Restore the *data* database (the memories, policies and workflows are still
   worth having).
2. Reset the local identity: `python -m scripts.reset_local_identity --database-url ... --yes`
   (opt-in, refuses non-local hosts, deletes agent keys and stale trust records).
3. Start the API. A new keypair and agent_id are generated.
4. **Re-establish trust out of band.** Every peer must re-fetch and re-verify the
   new card. There is no automatic path, by design - automatic re-trust is the
   vulnerability, not the convenience.

Prevention, in order of value:
1. Back the secret up out of band, separately from the database.
2. Rehearse the recovery on a copy, so the first time is not the incident.
3. Set `NEXUS_IDENTITY_KEY` explicitly in production. Note the startup warning
   when it is unset: session signing falls back to it.

---

## 6. Rehearsal checklist

Run this against a scratch environment. Each step should produce an artifact,
not a feeling.

- [ ] Take a dump. `pg_restore --list` parses it.
- [ ] Restore into a **new** database. Row counts match the source.
- [ ] `alembic current` in the restored database prints the expected revision.
- [ ] Start the API against the restored database. `/readyz` returns `ready`.
- [ ] `SELECT indisvalid FROM pg_index WHERE indexrelid::regclass::text LIKE '%hnsw%'` is `true`.
- [ ] Create an owner, register an agent, and confirm `/identity` returns a
      fingerprint that matches the pre-restore one (proves the identity secret
      was restored too, not just the rows).
- [ ] Restore the gateway database; confirm queued messages are still present
      and flush after a recipient reconnects.
- [ ] Deliberately lose `NEXUS_IDENTITY_KEY` in the scratch copy and confirm the
      app refuses to start with a clear message - then walk §5 once.
- [ ] Record the actual RTO and RPO you measured, and update §4 to match. **A
      target nobody has measured is a wish.**

---

## 7. What this runbook does not cover

- **Multi-region / failover.** Single Postgres instance, single region. No
  streaming replica is configured; recovery is restore-from-dump.
- **Encryption at rest.** Delegated to the host/volume, not implemented here.
- **Audit retention.** `activities` and the A2A audit grow without a retention
  policy; plan a reaper before the table is large (`MemoryRepository.reap_old`
  exists for memories only).
- **Automation.** Backups are manual commands. Nothing schedules them. Until
  something does, this document is a procedure someone must remember to run -
  which is the failure mode that makes every other section moot.
