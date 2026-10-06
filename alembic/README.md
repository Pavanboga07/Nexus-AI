# Alembic migrations

Migrations are numbered sequentially (`0001` → `0015`) and named after the
project **part** whose schema they introduce. The part numbers do not form a
contiguous range:

| Migration | Part | Notes |
|---|---|---|
| `0001_part2_memory` | Part 2 | Memory + pgvector |
| `0002_part3_identity` | Part 3 | Agent identity / keys |
| `0003_part4_policy` | Part 4 | Policy |
| `0004_part5_tools` | Part 5 | Tools |
| `0005_part6_a2a` | Part 6 | A2A messaging |
| `0006_part8_tasks` | Part 8 | Tasks |
| `0007_part9_workflows` | Part 9 | Workflows |
| `0008_part10_autonomy` | Part 10 | Autonomy |
| `0009_part12_orchestration` | Part 12 | Orchestration |
| `0010_part13_integration` | Part 13 | Integration |
| `0011_verified_agent_cards` | — | Hardening follow-up |
| `0012_auth` | M3 | Authentication |
| `0013_multi_agent` | M4 | Multi-agent |
| `0014_jobs` | M7 | Durable jobs |
| `0015_memory_scoping` | M8 | Memory scoping |

**Why parts 7 and 11 are missing:** Part 7 (gateway discovery) and Part 11
(NAT-traversal relay) added no new persistent schema — discovery state is
derived at runtime and the relay is an external service. There are no
`0007`-style gaps in the chain itself; the numbering is sequential and every
revision's `down_revision` links to its predecessor.
