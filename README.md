# Nexus Runtime

A personal AI agent system, built in parts. **This repository currently
contains Part 1 (Personal Agent Core), Part 2 (Persistent Personal Memory),
Part 3 (Cryptographic Agent Identity), Part 4 (Policy, Consent &
Minimum-Disclosure Engine), Part 5 (MCP-Compatible Tool System), Part 6
(Secure Agent-to-Agent Communication), Part 7 (Agent Discovery & Agent Cards),
and Part 8 (Agent Task Delegation & Negotiation). Parts 1–8 complete.**

Nexus is not "a wrapper around OpenAI". It is an agent runtime that *owns*
session state, conversation flow, configuration, and orchestration, and treats
the LLM as a replaceable reasoning component behind an interface. That
separation is what lets memory, policy, tools, identity, and agent-to-agent
communication be added later without rewriting the core.

---

## 1. What Part 1 implements

A working conversational agent core:

- A FastAPI HTTP API.
- In-process conversation sessions (create / get / clear).
- A provider-agnostic LLM adapter with an OpenAI implementation.
- A `NexusAgent` runtime that orchestrates a turn: load session → store user
  message → build context → call LLM → store reply → return reply.
- Environment-based configuration (no hard-coded keys).
- Structured logging of key events.
- Error handling that never leaks keys or stack traces.
- A test suite that mocks the LLM (no real API calls).

The milestone flow:

```text
User
  ↓
FastAPI
  ↓
Nexus Agent Runtime
  ↓
LLM Adapter
  ↓
LLM
  ↓
Nexus Agent Runtime
  ↓
FastAPI
  ↓
User
```

---

## 2. Architecture

The runtime owns state and behavior; the LLM is injected.

```text
                    NexusAgent
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   SessionStore    ContextBuilder   LLMProvider
   (state)         (prompt)         (reasoning)
                                        │
                                        ▼
                                  OpenAI-compatible
                                  (OpenAI / OpenRouter /
                                   Groq / Ollama / ...)
```

Key rule: **the LLM is not the agent.** `NexusAgent` depends only on the
`LLMProvider` interface. Swapping OpenAI for Anthropic, Google, or a local
model means adding one class and one branch in `build_provider` — the agent
runtime does not change.

Request lifecycle for `POST /chat`:

```text
route (validate) → agent.process_message()
    → sessions.get_session()          # 404 if unknown
    → sessions.add_message(user)      # persisted before the LLM call
    → context.build(session)          # [system, ...history]
    → provider.generate(messages)     # vendor call, errors normalised
    → sessions.add_message(assistant)
    → return reply
```

If the provider fails, the user's message stays in history so the turn can be
retried without losing what was said.

---

## 3. Project structure

```text
nexus/
├── app/
│   ├── __init__.py
│   ├── main.py                     # app factory, lifespan, logging, handlers
│   ├── api/
│   │   ├── __init__.py
│   │   ├── dependencies.py         # get_agent dependency
│   │   └── routes/
│   │       ├── __init__.py
│   │       └── chat.py             # health, sessions, chat routes
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── agent.py                # NexusAgent runtime (orchestration)
│   │   ├── session.py              # SessionStore interface + in-memory impl
│   │   └── context.py              # ContextBuilder (system prompt + history)
│   ├── llm/
│   │   ├── __init__.py             # build_provider factory
│   │   ├── base.py                 # LLMProvider ABC + error types
│   │   └── openai_adapter.py       # OpenAI-compatible adapter (only SDK user)
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── chat.py                 # Pydantic v2 request/response models
│   └── config/
│       ├── __init__.py
│       └── settings.py             # Pydantic settings + system prompt
├── tests/
│   ├── conftest.py                 # FakeProvider + app/client fixtures
│   ├── test_health.py
│   ├── test_chat.py
│   └── test_session.py
├── .env.example
├── .gitignore
├── pytest.ini
├── requirements.txt
├── README.md
└── run.py
```

### Important files and responsibilities

| File | Responsibility |
| --- | --- |
| `app/main.py` | Builds the object graph (`settings → provider → store → context → agent`), configures logging, installs exception handlers. |
| `app/agent/agent.py` | `NexusAgent`: the orchestrator. Owns the turn flow and session lifecycle passthroughs. Knows nothing about OpenAI. |
| `app/agent/session.py` | `SessionStore` interface and `InMemorySessionStore`. Temporary storage, clearly marked for replacement in Part 2. |
| `app/agent/context.py` | `ContextBuilder`: injects the system prompt and appends history. The seam where memory/identity/tools will be layered in. |
| `app/llm/base.py` | `LLMProvider` ABC, `UnconfiguredProvider`, and the `LLMError` hierarchy. |
| `app/llm/openai_adapter.py` | The only module that imports the OpenAI SDK. Works against any OpenAI-compatible endpoint; translates vendor errors into Nexus error types. |
| `app/llm/__init__.py` | `build_provider(settings)`: the single place provider selection happens. |
| `app/config/settings.py` | Pydantic settings; the only place environment variables are read. |
| `app/api/routes/chat.py` | Thin HTTP handlers: validate, delegate, map domain errors to status codes. |
| `app/schemas/chat.py` | Pydantic v2 models for every request/response. |

---

## 4. Installation

Requires **Python 3.12+**.

```bash
cd nexus
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

---

## 5. Environment setup

Copy the example file and fill in your key:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

`.env`:

```env
NEXUS_LLM_API_KEY=sk-...
NEXUS_LLM_MODEL=gpt-4o-mini
NEXUS_ENV=development
```

`.env` is git-ignored. **Never commit it.** If `NEXUS_LLM_API_KEY` is missing,
the server still starts and session endpoints work; `POST /chat` returns `503`
with a clear message until a key is configured.

### Using non-OpenAI providers

Nexus speaks the OpenAI-compatible chat-completions protocol, so any
compatible endpoint works by changing two variables:

| Provider | `NEXUS_LLM_BASE_URL` | Notes |
| --- | --- | --- |
| OpenAI (default) | *(leave unset)* | Official API. |
| OpenRouter | `https://openrouter.ai/api/v1` | Aggregator; free models have a `:free` suffix. |
| Groq | `https://api.groq.com/openai/v1` | Very fast; free tier with high rate limits. |
| Ollama (local) | `http://localhost:11434/v1` | Fully offline; key can be any non-empty string. |
| LM Studio (local) | `http://localhost:1234/v1` | Fully offline; key can be any non-empty string. |

Example (Groq):

```env
NEXUS_LLM_API_KEY=gsk-...
NEXUS_LLM_BASE_URL=https://api.groq.com/openai/v1
NEXUS_LLM_MODEL=openai/gpt-oss-20b
```

The provider name shown in `/health` and logs is derived from the base URL
(e.g. `groq`, `openrouter`, `ollama`). Some providers accept extra HTTP
headers (e.g. OpenRouter's attribution headers) via a JSON object:

```env
NEXUS_LLM_EXTRA_HEADERS={"HTTP-Referer": "https://yourapp.example", "X-Title": "Nexus"}
```

The legacy `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_BASE_URL` names still
work as fallbacks if the `NEXUS_LLM_*` equivalents are unset.

Other supported variables (see `.env.example` for the full list):

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXUS_LLM_MODEL` | `gpt-4o-mini` | Model used for reasoning. |
| `NEXUS_LLM_BASE_URL` | *(unset)* | OpenAI-compatible endpoint override. |
| `NEXUS_LLM_EXTRA_HEADERS` | *(unset)* | JSON object of extra HTTP headers. |
| `NEXUS_ENV` | `development` | `development` enables autoreload. |
| `NEXUS_HOST` / `NEXUS_PORT` | `127.0.0.1` / `8000` | Bind settings for `run.py`. |
| `NEXUS_LLM_TIMEOUT` | `60` | Timeout (seconds) for one LLM call. |
| `NEXUS_MAX_SESSION_MESSAGES` | `100` | History cap per session; oldest are trimmed. |
| `NEXUS_LOG_LEVEL` | `INFO` | Root log level. |
| `NEXUS_SYSTEM_PROMPT` | *(built-in)* | Overrides the agent's system identity. |

---

## 6. How to run the server

```bash
python run.py
```

or with autoreload:

```bash
uvicorn app.main:app --reload
```

The server listens on `http://127.0.0.1:8000` by default. Interactive API docs
are at `/docs`.

---

## 7. API endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness + configuration probe. |
| `POST` | `/sessions` | Create a session, returns a UUID. |
| `GET` | `/sessions/{session_id}` | Retrieve a session and its messages. |
| `DELETE` | `/sessions/{session_id}` | Clear a session's history (id stays valid). |
| `POST` | `/chat` | Send a message, get the assistant's reply. |

### Example requests

Health:

```bash
curl http://127.0.0.1:8000/health
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "environment": "development",
  "llm_provider": "groq",
  "llm_configured": true
}
```

Create a session:

```bash
curl -X POST http://127.0.0.1:8000/sessions
```

```json
{ "session_id": "fbcd9d1b-d559-4bd2-a488-c09b02eeaeb5" }
```

Chat:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "fbcd9d1b-...", "message": "Hi, my name is Boss."}'
```

```json
{
  "session_id": "fbcd9d1b-...",
  "response": "Hello Boss! How can I help you today?"
}
```

Continue the same session (context is preserved):

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "fbcd9d1b-...", "message": "What is my name?"}'
```

```json
{
  "session_id": "fbcd9d1b-...",
  "response": "Your name is Boss."
}
```

Retrieve / clear:

```bash
curl http://127.0.0.1:8000/sessions/fbcd9d1b-...
curl -X DELETE http://127.0.0.1:8000/sessions/fbcd9d1b-...
```

### Error responses

Errors use a consistent shape and never include stack traces or credentials:

```json
{ "error": "validation_error", "detail": "Request body failed validation." }
```

| Status | When |
| --- | --- |
| `404` | Unknown session id. |
| `422` | Empty/blank message or missing field. |
| `502` | Provider returned an error. |
| `503` | Provider not configured (missing API key). |
| `504` | Provider timed out. |
| `500` | Unexpected error (generic message; details go to logs only). |

---

## 8. How to run tests

```bash
python -m pytest -v
```

Tests never call the real OpenAI API. `tests/conftest.py` provides a
`FakeProvider` that records the context it receives, so tests can assert that
conversation history is actually passed to the LLM. Failure cases (missing
session, blank message, provider errors) are covered too.

Coverage by file:

- `tests/test_health.py` — health endpoint; provider factory with/without a key.
- `tests/test_session.py` — store-level and HTTP-level session lifecycle, trimming, 404s.
- `tests/test_chat.py` — reply round-trip, context persistence across turns, error mapping.

---

## 9. Intentionally NOT implemented yet

Part 1 is deliberately minimal. The following are **out of scope** and absent
by design:

- Persistent memory, PostgreSQL, pgvector, vector search.
- Agent identity, policy, consent.
- MCP tools, A2A, networking/gateway.
- Authentication, multi-agent support, WebSockets.
- Redis, Celery, Kafka, Kubernetes, Docker orchestration.
- LangChain / LangGraph.

Sessions live in process memory only: restarting the server loses them, and the
store is per-worker (it will not work behind multiple Uvicorn workers). This is
documented in `app/agent/session.py` and is the first thing Part 2 replaces.

---

## 10. Roadmap

| Part | Focus | Where it plugs in |
| --- | --- | --- |
| 1 | Personal Agent Core | *(this repository)* |
| 2 | Persistent Memory | Replace `InMemorySessionStore` with a PostgreSQL + pgvector `SessionStore`; add retrieval to `ContextBuilder`. |
| 3 | Agent Identity | Feed an identity module into `ContextBuilder` in place of the configurable system prompt. |
| 4 | Policy & Consent | Insert a policy check in `NexusAgent` between context build and provider call. |
| 5 | MCP Tools | Add a tool registry the agent can invoke; extend the provider interface with tool-call support. |
| 6 | A2A | Add a peer-communication module alongside `LLMProvider`; add routes. |
| 7 | Network Gateway | Add a gateway layer in front of the FastAPI app. |
| 8 | UI | Consume the existing HTTP API; no runtime changes required. |

Each part depends only on the interfaces already defined here, which is why the
seams (`SessionStore`, `LLMProvider`, `ContextBuilder`, `NexusAgent`) exist now.

---

## 11. Design decisions

- **LLM is injected, not imported.** The agent depends on an ABC; only
  `app/llm/openai_adapter.py` touches the OpenAI SDK, and it speaks the
  OpenAI-compatible protocol so the same adapter serves OpenAI, OpenRouter,
  Groq, and local runtimes. This is the single most important structural
  choice.
- **`UnconfiguredProvider` instead of a startup crash.** With no API key the
  runtime still serves sessions and `/health`; only chat fails, with `503`. This
  makes local development and CI friction-free and keeps the failure honest.
- **System prompt is injected at context-build time**, not stored in the
  session. It can never be trimmed away, and changing it takes effect at once.
- **User message is persisted before the LLM call.** A failed turn does not
  lose what the user said; they can retry.
- **History trimming.** Sessions are capped at `NEXUS_MAX_SESSION_MESSAGES` to
  bound memory use until real memory management arrives in Part 2.
- **`DELETE /sessions/{id}` clears rather than destroys.** The spec called it
  "clear session", so the id stays valid and the conversation can continue with
  a clean history.

---

## 12. Known limitations

- ~~Sessions are in-process and non-durable~~ **Fixed in Part 2**: sessions and
  memories persist to PostgreSQL; they survive restarts.
- No streaming responses yet — replies arrive whole.
- No token accounting or cost tracking.
- No rate limiting.
- The `openai` SDK is pinned to a version compatible with the installed
  `httpx`; keep both current together.

---

# Part 2 — Persistent Personal Memory

Part 2 replaces the in-process session store with PostgreSQL and adds a
long-term personal memory system: extraction, embeddings (pgvector), semantic
retrieval, and injection into the LLM context. The Part 1 API is unchanged.

## Architecture

```text
                         USER
                           │
                           ▼
                    FastAPI API
                           │
                           ▼
                  ┌─────────────────┐
                  │  Nexus Agent    │
                  │     Runtime     │
                  └────────┬────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
              ▼            ▼            ▼
          Context       Memory        LLM
          Builder       Manager      Provider
              │            │
              │      ┌─────┴─────┐
              │      ▼           ▼
              │ PostgreSQL   pgvector
              │
              └──────────┐
                         ▼
                       LLM
                         │
                         ▼
                      Response
```

Per chat turn:

```text
POST /chat
  ↓
load conversation (PostgreSQL)
  ↓
embed user message → pgvector cosine KNN → top-K memories (owner-scoped)
  ↓
context = system prompt + memory block + conversation history
  ↓
LLM → reply (returned to user immediately)
  ↓
background task: LLM extraction → validation → dedup → embed → PostgreSQL
```

Memory is NOT conversation history. The extractor distils durable facts
(semantic preferences, episodic events, relationships) from each turn; chat
transcripts stay in `conversations`/`messages`, extracted facts live in
`memories` with vector embeddings.

## Memory model

| memory_type | Meaning | Example |
| --- | --- | --- |
| `semantic` | Stable facts/preferences | "Boss prefers meetings after 6 PM." |
| `episodic` | Events that happened | "Boss and Rahul discussed the Nexus architecture." |
| `relationship` | People and relations | "Rahul is Boss's college project partner." |

Every row carries `importance` and `confidence` (LLM-suggested, clamped to
0–1), `last_accessed_at` (updated on every retrieval), JSONB `metadata`, and
an optional `source_message_id` link back to the originating chat message.

## Project structure (Part 2 additions)

```text
app/
├── database/
│   ├── connection.py        # async engine + session factory
│   ├── models.py            # Owner / Conversation / Message / Memory
│   ├── repositories.py      # all SQL lives here (owner-scoped)
│   └── session_store.py     # DatabaseSessionStore (Part 1 interface)
├── memory/
│   ├── manager.py           # MemoryManager: store/search/dedup facade
│   ├── extractor.py         # LLM structured output → validated candidates
│   └── embeddings.py        # EmbeddingProvider ABC + OpenAI/local impls
├── schemas/memory.py        # /memories API models
└── api/routes/memories.py   # GET /memories, POST /memories/search, DELETE
alembic/versions/0001_part2_memory.py   # schema + pgvector + indexes
docker-compose.yml           # pgvector/pgvector:pg16 on host port 5433
```

## Local development setup

1. **Start PostgreSQL** (Docker, includes pgvector):

   ```bash
   docker compose up -d
   ```

   Host port **5433** (5432 is often taken); credentials `nexus:nexus`,
   database `nexus`. Data persists in the `nexus_nexus_pgdata` volume.

2. **Configure `.env`** (see `.env.example`): set `DATABASE_URL`, and pick an
   embedding provider:

   - `NEXUS_EMBEDDING_PROVIDER=local` — deterministic hash embedder, no
     network, no cost. **No real semantics** (n-gram overlap only), fine for
     development. Use this with Groq (no embeddings API).
   - `NEXUS_EMBEDDING_PROVIDER=openai` — real semantic vectors via
     `text-embedding-3-small` (1536 dims); requires an OpenAI-compatible
     endpoint that offers `/embeddings` and an API key.

3. **Run migrations**:

   ```bash
   python -m alembic upgrade head
   ```

4. **Start Nexus**: `python run.py` — `/health` now reports `database` and
   `memory` status. If PostgreSQL is down, Nexus still starts on the
   Part 1 in-memory fallback (visible in `/health`).

5. **Run tests**: `python -m pytest -v` — DB tests create a scratch
   `nexus_test` database automatically and skip gracefully if the container
   is not running.

## Memory endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/memories?memory_type=&limit=` | List memories (owner-scoped). |
| `POST` | `/memories/search` | `{"query": "...", "limit": 5}` semantic search with similarity scores. |
| `DELETE` | `/memories/{id}` | Delete one memory. |

## How deduplication works

Before storing a candidate, its embedding is compared against existing
memories (same owner) via cosine distance. If similarity ≥
`NEXUS_MEMORY_DEDUP_THRESHOLD` (default 0.92), the existing row is refreshed
(content updated, importance/confidence raised to the max) instead of
inserting a near-identical row. Below the threshold, a new memory is stored.

With the local hash embedder, only near-identical strings reach the
threshold; with OpenAI embeddings, genuinely paraphrased duplicates also
merge. This is the simple, spec-appropriate strategy — no knowledge graph.

## Owner isolation

Every repository method takes `owner_id` and every query filters on it. There
are no global memory reads anywhere in the codebase. Part 2 assumes a single
implicit owner (created lazily), but the schema and all queries are
owner-scoped so multi-user and Part 4 policy enforcement are additive, not
rewrites. Remote agents will reach memory only through the runtime (A2A →
policy → MemoryManager), never the database.

## Part 2 limitations

- **Local embeddings are not semantic.** Similarity ≈ character n-gram
  overlap. Retrieval quality with `local` is therefore modest; production
  quality needs the OpenAI embedder.
- **Extraction is best-effort.** One LLM call per turn (background); small
  models sometimes return unparsable JSON, which is safely dropped. The demo
  run showed an occasional paraphrase slipping past dedup with local
  embeddings (0.44 similarity < 0.92 threshold) — expected with n-gram
  vectors.
- **Exact KNN scan.** pgvector cosine search is a sequential scan; an HNSW
  index needs a fixed dimension, so it's deferred until the embedder choice
  is pinned. Fine at personal-memory scale (thousands of rows).
- **Memory extraction is fire-and-forget** on the event loop, not a durable
  queue; a crash mid-extraction loses that turn's candidates.
- Alembic migration and `Base.metadata.create_all` (used by tests) are
  separate paths; schema changes must update both (or tests should be pointed
  at migrated schemas in future).

---

# Part 3 — Cryptographic Agent Identity

Every Nexus installation owns a persistent **Ed25519 identity** that belongs
to the agent — not to the LLM provider, model, or any conversation. Changing
`NEXUS_LLM_MODEL` or swapping providers never changes `agent_id` or the
public key.

```text
                         NEXUS AGENT
                              │
          ┌───────────────────┼──────────────────┐
          ▼                   ▼                  ▼
      Identity             Memory              LLM
          │                   │              Provider
      Ed25519           PostgreSQL + pgvector
          │
    Agent ID / Public Key / (encrypted) Private Key
```

## Identity model

| Field | Meaning |
| --- | --- |
| `agent_id` | `nexus:ed25519:<first 16 bytes of SHA-256(raw public key) as hex>` — deterministic and independently verifiable from the public key alone. |
| `public_key` | base64 of the raw 32-byte Ed25519 public key. |
| `encrypted_private_key` | base64 of `nonce ‖ AES-256-GCM ciphertext+tag`. Never plaintext, never in API responses or logs. |
| `key_algorithm` | `Ed25519`. |
| `fingerprint` | Human check format: `EAD8-5CBD-...` (8 groups of 4 uppercase hex from SHA-256 of the public key). |

## Key encryption at rest

The private key is encrypted with **AES-256-GCM** before it touches the
database. The encryption secret comes from `NEXUS_IDENTITY_KEY` in the
environment (generate with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`). The secret
is derived to an AES key via SHA-256 (sufficient: the secret is high-entropy,
not a password).

**Security trade-off (documented per spec):** the database stores ciphertext
only, and GCM authenticates it — tampering or a wrong secret fails loudly at
startup and the application **refuses to start** rather than silently
generating a replacement identity (which would break all future trust
relationships). The secret itself must be kept out of Git; if it is lost, the
identity is unrecoverable by design. Back up `NEXUS_IDENTITY_KEY` alongside
your database backups.

## Startup behaviour

```text
load owner -> load identity row
  -> exists? YES: decrypt -> check keypair consistency -> check agent_id
                     -> sign/verify self-test -> ready
  -> exists? NO : generate keypair -> derive agent_id -> encrypt -> persist
```

Any failure in the YES path (wrong/missing secret, corrupted data, mismatched
keypair, failed self-test) aborts startup with a clear error. Without a
database the app still runs; identity endpoints return 503.

## Project structure (Part 3 additions)

```text
app/
├── identity/
│   ├── crypto.py          # Ed25519 + AES-GCM primitives (only crypto code)
│   ├── serialization.py   # canonical JSON for future signed messages
│   ├── service.py         # IdentityService + PublicIdentity
│   └── models.py          # AgentIdentity ORM model
├── api/routes/identity.py # GET /identity, POST /identity/verify
└── schemas/identity.py
alembic/versions/0002_part3_identity.py
tests/test_identity.py, test_identity_crypto.py, test_identity_api.py
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/identity` | Public identity only: agent_id, public_key, algorithm, fingerprint. |
| `POST` | `/identity/verify` | Verify (agent_id, public_key, message, signature). Validates the claimed agent_id against the supplied public key — not just the signature. |

`/health` now reports `"identity": true/false`.

Canonical signing helper for future A2A messages:
`app/identity/serialization.py::canonical_json_bytes` — deterministic key
order, compact UTF-8, floats rejected (platform-dependent repr).

## Owner scoping

The `agent_identities` table is keyed by `owner_id` (unique per owner, FK to
`owners`). All identity loads are owner-scoped; tests verify two owners get
distinct identities and never see each other's. Multi-agent identity
management is intentionally not implemented (schema supports it).

## Migration

```bash
python -m alembic upgrade head   # applies 0001 (memory) then 0002 (identity)
```

## Part 3 limitations

- Single local identity per owner; no rotation, revocation, or export/import
  flows yet.
- `verify` is provided as a local endpoint for testing; remote verification
  arrives with Part 6 A2A.
- If `NEXUS_IDENTITY_KEY` is lost, the identity cannot be recovered — by
  design. Keep the secret backed up.
- The identity is not yet used to sign anything in the chat/memory flow;
  it is foundational infrastructure for later parts.

---

# Part 4 — Policy, Consent & Minimum-Disclosure Engine

Nexus can now answer two questions deterministically, with **no LLM
involvement**:

- *"Is Nexus allowed to disclose this information to this requester for
  this purpose?"*
- *"Is Nexus allowed to perform this action on behalf of the owner?"*

**The central principle: the LLM may interpret a request, but the LLM must
never be the final authority for authorization.** The engine is pure,
deterministic logic: same inputs + same database state ⇒ same decision.

## Security model

```text
Identity = Who are you?
Policy   = What are you allowed to do?
Consent  = What did the owner explicitly approve?
Memory   = What does the agent know?
LLM      = How does the agent reason?
```

The security boundary — no request reaches Memory/Tools without passing the
engine:

```text
External Request -> Identity -> Policy Engine -> ALLOW/ASK/DENY -> Agent
```

## Decision model

`PolicyDecision` enum: **ALLOW**, **ASK**, **DENY**, plus a
`disclosure_scope` (none / category / summary / exact) — the minimum
disclosure that may be sent.

**Defaults:** no matching rule → **ASK** (never ALLOW). The sensitive
categories `financial` and `private` are **deny-by-default**: even a
wildcard ALLOW-everything policy cannot unlock them — only an
exact-category policy or explicit consent can.

## Policy precedence (deterministic)

Most specific first; the first matching level wins:

1. exact requester + exact data + exact action + exact purpose
2. exact requester + exact data + exact action
3. exact requester + exact data
4. exact requester
5. wildcard requester + exact data + exact action + exact purpose
6. wildcard requester + exact data + exact action
7. wildcard requester + exact data
8. global default (all wildcards)
9. no rule → ASK (or DENY for sensitive categories)

Within one specificity level: **higher `priority` wins**; at equal priority
**DENY beats ASK beats ALLOW** — a broad ALLOW can never override an
explicit DENY unless a strictly higher-priority rule says so. Expired rules
never match; rules with a future `starts_at` are not yet active.

## Purpose limitation

Permission is tied to a purpose. "ALLOW Rahul, availability, scheduling"
does **not** mean "ALLOW Rahul, availability, marketing". A purpose
mismatch makes the rule non-matching and evaluation continues; if nothing
else applies → ASK (or DENY).

## Consents

Consents record the owner's explicit answer to an ASK (or any explicit
grant/denial). They are always concrete — no wildcards. Types:

- **one-time** (`single_use: true`): consumed atomically on the ALLOW that
  uses it; a concurrent second request can never also consume it
  (`UPDATE ... WHERE used_at IS NULL` under PostgreSQL row locking).
- **time-limited** (`expires_at`).
- **persistent** (neither).

Duration is always explicit in the API request — nothing implicit.

## Audit trail

Every evaluation (ALLOW, ASK, DENY alike) writes a `policy_decisions` row:
who asked, for what category/action/purpose, the decision, and which
policy/consent matched. Decision metadata only — **never disclosed
content**.

## Owner isolation

All policy/consent/audit queries are owner-scoped at the repository level;
there are no unscoped reads anywhere. Tests prove owners cannot see,
delete, or consume each other's rules, consents, or audit records, and that
a requester's identity never bypasses another owner's isolation.

## API endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/policy` | List owner policies. |
| `POST` | `/policy` | Create a policy rule. |
| `DELETE` | `/policy/{id}` | Delete a policy. |
| `GET` | `/consent` | List owner consents. |
| `POST` | `/consent` | Record an explicit consent (allow once / until / forever, or deny). |
| `DELETE` | `/consent/{id}` | Revoke a consent. |
| `POST` | `/policy/evaluate` | Evaluate an authorization request. |
| `GET` | `/policy/audit` | Owner-scoped decision audit trail. |

Example: create a rule, then evaluate — scheduling ALLOWs, marketing does not:

```bash
curl -X POST localhost:8000/policy -H "Content-Type: application/json" -d '{
  "requester_agent_id": "nexus:ed25519:rahul",
  "data_category": "availability",
  "action": "disclose_information",
  "purpose": "scheduling",
  "decision": "ALLOW",
  "disclosure_scope": "summary"
}'

curl -X POST localhost:8000/policy/evaluate -H "Content-Type: application/json" -d '{
  "requester_agent_id": "nexus:ed25519:rahul",
  "data_category": "availability",
  "action": "disclose_information",
  "purpose": "scheduling"
}'
# -> {"decision":"ALLOW", "disclosure_scope":"summary", ...}

# same but "purpose": "marketing"  ->  {"decision":"ASK", ...}
```

## Validation

Boundary validation on every field: slug vocabulary (lowercase letters,
digits, `- : _ .`), length caps, no surrounding whitespace, enum-checked
decision/scope, `expires_at > starts_at`, and injection payloads
(`DROP TABLE`, `'; --`, `<script>`) rejected with 422. Unknown but
well-formed categories are accepted so future categories need no code
change. Evaluation requests may never use the wildcard `*` as a requester.

## Project structure (Part 4 additions)

```text
app/
├── policy/
│   ├── models.py       # Policy, Consent, PolicyDecisionRecord + enums
│   ├── engine.py       # pure deterministic evaluation (no SQL, no LLM)
│   ├── repository.py   # owner-scoped SQL, atomic consent consumption
│   └── service.py      # PolicyService facade + validation
├── api/routes/policy.py
└── schemas/policy.py
alembic/versions/0003_part4_policy.py
tests/test_policy_engine.py, tests/test_policy_api.py
```

## Future A2A integration seam

```text
Incoming A2A request
    -> verify cryptographic identity (Part 3)
    -> PolicyService.authorize()        (this part)
    -> minimum disclosure scope
    -> retrieve only permitted data
    -> send response
```

The invariant enforced going forward: **no remote agent, LLM output, memory
retrieval, or future tool call may bypass the Policy & Consent Engine.**

## Part 4 limitations

- The chat flow does not yet consult the engine (no external requesters
  exist yet); it becomes the gatekeeper when A2A arrives in Part 6.
- `disclosure_scope` is decided but not enforced against retrieved content
  (no field-level redaction yet).
- No approval UI; ASK returns `requires_user_approval: true` and consents
  are recorded via API.
- No policy import/export or bulk management.

---

# Part 5 — MCP-Compatible Tool System

Nexus now has a tool subsystem built on MCP concepts: structured tool
metadata (name, description, inputSchema), discovery, strict argument
validation, and controlled execution. It is an *internal* MCP-compatible
abstraction — remote MCP servers, transports, OAuth, sampling, and resources
are deliberately out of scope for now.

**The core rule:**

> MCP does not grant permission. It only describes and exposes capabilities.
> Nexus Policy decides whether those capabilities may be used.

And the execution invariant:

```text
TOOL REQUEST -> TOOL SERVICE -> POLICY SERVICE
                                 ├── ALLOW -> EXECUTE -> RESULT -> AUDIT
                                 ├── ASK   -> APPROVAL REQUIRED
                                 └── DENY  -> REJECT
```

**The LLM may request a tool. The LLM never directly executes one.**
(`POST /chat` is unchanged in Part 5 — LLM tool-calling integration is a
later, controlled step.)

## Architecture

```text
app/tools/
├── registry.py    # BaseTool contract + ToolRegistry (name validation,
│                  #   discovery; NO execute method - by design)
├── schemas.py     # ToolInvocation / ToolContext / ToolResult
├── executor.py    # safe boundary: asyncio timeout, result-size cap,
│                  #   exception isolation
├── service.py     # ToolService: validate -> policy -> execute -> audit
├── models.py      # tool_executions audit table
├── errors.py      # structured ToolError codes
└── builtin/       # echo + get_current_time (safe demo tools)
```

Five separated responsibilities after Part 5:

```text
Identity  "What agent is this?"
Memory    "What does this agent know?"
Policy    "What is this agent allowed to do?"
Tools     "What capabilities does this agent have?"
LLM       "How should this agent reason?"
```

## Tool contract

Every tool declares a Pydantic `args_model` with `extra="forbid"` — the model
IS the inputSchema, so metadata and validation can never drift. Unknown
arguments, missing required fields, wrong types, and oversized values are
rejected **before** policy evaluation and execution.

Tool names: `^[a-z][a-z0-9_-]{0,63}$` (blocks path traversal, shell
metacharacters, SQL, spaces). Duplicate registration fails clearly; listing
is deterministic (sorted).

`ToolContext` gives a tool ONLY `owner_id`, `request_id`, `purpose` — never
database sessions, secrets, conversation history, or credentials. The owner
context comes from the authenticated caller, never from tool arguments.

## Policy integration mapping

Every execution evaluates the Part 4 engine with:

```text
requester_agent_id = "nexus:self"   (the agent itself; external requesters
                                      arrive with A2A in Part 6)
action             = "access_tool"
data_category      = tool.data_category (built-ins: "custom")
purpose            = invocation.purpose (mandatory, flows into policy)
```

So all Part 4 semantics apply: no rule → ASK (approval_required, nothing
executes), explicit ALLOW → execute, DENY → reject, purpose limitation,
expiry, consents, sensitive defaults, owner isolation.

## Execution guards

- **Timeout** (`NEXUS_TOOL_TIMEOUT_SECONDS`, default 10): a hung tool
  returns `TIMEOUT` instead of hanging the agent.
- **Result size** (`NEXUS_TOOL_MAX_RESULT_BYTES`, default 65536): oversized
  results fail safely with `RESULT_TOO_LARGE` — no truncation (truncating
  could corrupt meaning; documented trade-off).
- **Exception isolation**: a tool that raises returns
  `EXECUTION_ERROR` with no stack trace or internals; the crash is logged
  server-side only. Nexus keeps running.

## Audit

`tool_executions` records every invocation: request_id, tool, purpose,
status (`approval_required` / `allowed` / `denied` / `executed` / `failed`),
policy decision, error code, timestamps. **Never stores arguments or tool
output** — the audit proves who/what/why/authorized/executed without
becoming a sensitive data dump. Owner-scoped.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/tools` | MCP-style metadata for all tools. |
| `GET` | `/tools/{name}` | One tool's metadata (404 when unknown). |
| `POST` | `/tools/execute` | Policy-gated execution request. |
| `GET` | `/tools/audit/list` | Owner-scoped execution audit trail. |

Example session:

```bash
# 1. No policy -> nothing executes
curl -X POST localhost:8000/tools/execute -H "Content-Type: application/json" -d '{
  "tool_name": "echo", "arguments": {"text": "hello"},
  "request_id": "req_1", "purpose": "testing"
}'
# -> {"success": false, "status": "approval_required", ...}

# 2. Owner grants permission
curl -X POST localhost:8000/policy -H "Content-Type: application/json" -d '{
  "requester_agent_id": "nexus:self", "data_category": "custom",
  "action": "access_tool", "purpose": "testing", "decision": "ALLOW"
}'

# 3. Same request now executes
# -> {"success": true, "status": "executed", "data": {"text": "hello"}, ...}
```

## Security invariants (tested)

- No tool executes without an ALLOW from the policy engine — there is no
  code path around it (the registry has no execute; only ToolService
  executes, and it always evaluates policy first).
- A newly registered tool is NOT automatically executable (ASK default).
- Tools cannot access the database, filesystem, shell, or secrets; no
  eval/exec/subprocess anywhere in the subsystem.
- Arguments and output never appear in audit records; secrets never appear
  in metadata, results, audit, or logs.
- Owner A's policy cannot make Owner B's invocations execute.

## Migration

```bash
python -m alembic upgrade head   # 0001 -> 0002 -> 0003 -> 0004
```

## Part 5 limitations

- Two built-in demo tools only (`echo`, `get_current_time`); real tools
  (calendar, email, …) come later.
- `/chat` does not yet invoke tools — LLM tool-calling integration is a
  later controlled step (per spec).
- Registry is in-process; no dynamic tool loading or remote MCP yet.
- `data_category` per tool is a class attribute (static mapping);
  finer-grained per-argument policy comes with the policy refinement later.
- The `allowed` audit status exists for completeness but ALLOW transitions
  directly to `executed`/`failed` in the current flow.

---

## 8. What Part 7 implements: Agent Discovery & Agent Cards

Self-sovereign, verifiable self-descriptions (**Agent Cards**) and automated discovery:

- `AgentCard` data structure: protocol, version, agent_id, display_name, public_key, endpoint, capabilities, supported_purposes, issued_at, expires_at, signature.
- Stripping implementation secrets (`inputSchema`) while advertising capabilities.
- Ed25519 signing and verification over canonical JSON (no floats).
- Public discovery endpoints: `GET /.well-known/nexus-agent.json` and `GET /a2a/card`.
- Authenticated onboarding: `POST /a2a/discover` with full SSRF protection, timeout, size limits, and key matching.
- Invariant: **Discovered ≠ Trusted; Trusted ≠ Authorized**.

---

## 9. What Part 8 implements: Agent Task Delegation & Negotiation

Secure Agent-to-Agent task delegation, human approval, and bounded multi-round negotiation:

```text
User A
   ↓
Agent A  (Requester)
   ↓  (Signed A2A Envelope: task_request)
Agent B  (Responder)
   ↓  1. Schema, Signature, Replay & Trust checks
   ↓  2. PolicyService evaluation (ALLOW / ASK / DENY)
   ↓  3. TaskContext & TaskHandler execution
   ↓  4. Minimum-disclosure response
Agent A
   ↓
User A
```

### Key Components

1. **Task Model & Lifecycle**:
   - Extended `a2a_tasks` model (`task_type`, `purpose`, `request_payload`, `response_payload`, `completed_at`, `failure_reason`, `negotiation_round`).
   - Lifecycle: `PENDING` → `ACCEPTED` / `PENDING_APPROVAL` → `COMPLETED` / `REJECTED` / `FAILED` / `CANCELLED`.
2. **Task Handlers**:
   - `AvailabilityCheckHandler` (`availability_check`): Evaluates calendar/schedule memories, returning minimal disclosure (`{"available": bool}` or `{"available": bool, "alternative_times": [...]}`). Never exposes event titles or notes.
   - `MeetingProposalHandler` (`meeting_proposal`): Proposes meeting slots; returns `accepted` or `counter_proposal`.
   - `InformationRequestHandler` (`information_request`): Query-based memory retrieval bounded by `disclosure_scope`.
3. **Bounded Negotiation**:
   - Multi-round proposal exchanges (`task_proposal`).
   - Hard limits: `MAX_NEGOTIATION_ROUNDS = 3` (configurable), TTL expiration checks.
   - **Negotiation never overrides policy**: Every round and data category is independently verified and authorized.
4. **Human Approval Flow**:
   - Policy `ASK` transitions task to `pending_approval`.
   - Explicit owner APIs: `POST /a2a/tasks/{task_id}/approve` and `POST /a2a/tasks/{task_id}/reject`.
5. **Idempotency & Replay Protection**:
   - Duplicate message IDs rejected.
   - Re-sent task requests return cached results without duplicate execution.

### API Reference

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/a2a/tasks` | Delegate a task to a trusted agent |
| `GET` | `/a2a/tasks` | List owner-scoped tasks (optional `?status=` filter) |
| `GET` | `/a2a/tasks/{task_id}` | Get single task details |
| `POST` | `/a2a/tasks/{task_id}/approve` | Approve a `pending_approval` task |
| `POST` | `/a2a/tasks/{task_id}/reject` | Reject a task |
| `POST` | `/a2a/tasks/{task_id}/cancel` | Cancel an active task |
| `POST` | `/a2a/tasks/{task_id}/negotiate` | Submit a counter-proposal / next round |

### Database Migration

```bash
python -m alembic upgrade head   # 0001 -> ... -> 0005 -> 0006_part8_tasks -> 0007_part9_workflows
```

---

## 10. What Part 9 implements: Proactive Workflows & Agent Orchestration

Part 9 introduces a durable, persistent, resumable **Workflow Layer** above individual tasks:

```text
User
  ↓
Personal Agent
  ↓
Workflow Engine (Multi-Step DAG / Sequence)
  ├── Step 1: Availability Check (Local Calendar / Memories)
  ├── Step 2: A2A Task Delegation (Remote Agent Availability Query)
  ├── Step 3: Candidate Selection (Intersection Computation)
  ├── Step 4: A2A Task Delegation (Propose Meeting to Remote Agent)
  └── Step 5: Completed Coordination Result
```

### Core Architecture & Invariants

1. **Workflow vs Task**:
   - A **Task** (Part 8) is a single, bounded operation between two agents or an agent and a tool (e.g., `availability_check`).
   - A **Workflow** (Part 9) is a durable state machine orchestrating multiple steps over time, managing accumulated context, pausing for approvals or remote responses, and recovering after crashes.
2. **Model is NOT the Workflow Participant**:
   - The workflow engine orchestrates deterministic application actions.
   - Workflow definitions are strictly structured Pydantic models. No arbitrary Python code execution (`eval()` / `exec()`).
3. **Independent Policy Boundaries (`Step 1 ALLOW ≠ Step 2 ALLOW`)**:
   - Every single step is independently evaluated against `PolicyService`.
   - Workflows cannot escalate privilege. Authorizing Step 1 does not automatically authorize Step 2.
   - If a step produces `DENY`, the workflow fails immediately without retry.
   - If a step produces `ASK`, the workflow pauses into `WAITING_APPROVAL`.
4. **Approval & Human-in-the-Loop**:
   - When in `WAITING_APPROVAL`, owner calls `POST /workflows/{id}/approve`.
   - Single-use consent is created for the step, and the workflow safely resumes execution.
5. **Durable Persistence & Crash Recovery**:
   - All state is persisted across `workflows` and `workflow_steps` PostgreSQL tables.
   - Upon restart, `recover_interrupted_workflows()` scans for workflows in `RUNNING` status and steps interrupted mid-execution, safely resets them to `PENDING` within retry limits, and resumes execution without re-executing already completed steps.
6. **Concurrency Protection**:
   - Workers acquire steps atomically using `SELECT ... FOR UPDATE` row-level database locking (`acquire_next_pending_step`).
   - Dual-worker duplicate step pickup is strictly prevented.
7. **Bounded Retries**:
   - Bounded by `MAX_STEP_ATTEMPTS = 3`.
   - Only transient errors (network timeouts, transient database drops) trigger retry.
   - Security failures (policy `DENY`, invalid signatures, revoked agents) fail immediately with zero retries.
8. **Tool & A2A Integration**:
   - Steps re-use Part 5 `ToolService` and Part 8 `A2AService.delegate_task()`.
   - When delegating an asynchronous A2A task, the step enters `WAITING` and workflow enters `WAITING_REMOTE` until response arrives.
9. **Minimum Disclosure Privacy Model**:
   - Demonstrated in the `meeting_coordination` workflow:
     - User A's full calendar is never sent to Rahul.
     - Rahul's full calendar is never sent to User A.
     - Only candidate mutual availability (`{"available": true, "candidate_time": "18:00"}`) is exchanged.
10. **Audit & Observability**:
    - Structured audit logs emitted for: `workflow_created`, `workflow_started`, `step_started`, `step_completed`, `step_failed`, `step_retry`, `workflow_waiting`, `workflow_resumed`, `workflow_completed`, `workflow_cancelled`, `workflow_expired`.

### API Reference

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/workflows` | Create a new workflow definition with structured steps |
| `GET` | `/workflows` | List owner-scoped workflows (optional `?status=` filter) |
| `GET` | `/workflows/{workflow_id}` | Get workflow status, context, and detailed step states |
| `POST` | `/workflows/{workflow_id}/start` | Start execution of a pending workflow |
| `POST` | `/workflows/{workflow_id}/approve` | Approve a step in `waiting_approval` status |
| `POST` | `/workflows/{workflow_id}/cancel` | Cancel an active workflow (skips pending steps) |

### Test Suite

Comprehensive test suite in `tests/test_workflows.py`:
- Creation & validation (no arbitrary code, unknown step rejection)
- Policy enforcement (ALLOW, ASK pause, DENY halt, independent step authorization)
- Owner approval flow & isolation across owners
- Crash recovery and replay avoidance
- Concurrency protection via row locking
- Bounded retries for transient failures vs immediate failure for security errors
- Expiration and cancellation semantics
- Local Tool integration via `ToolService`
- End-to-end `meeting_coordination` demo with strict minimum disclosure
- HTTP API lifecycle endpoints

```bash
# Run Part 9 workflow tests
python -m pytest tests/test_workflows.py -v

# Run full regression suite (399 tests)
python -m pytest -q
```

---

## 11. Nexus Frontend: Personal AI Command Center

A real-time, production-ready frontend built with **Next.js 14 (App Router)**, **TypeScript**, **Tailwind CSS**, and **Lucide React** icons that surfaces all backend capabilities into a unified personal AI dashboard.

### Architecture

```text
User Browser (http://localhost:3000)
    │
    ├── Overview (/) ─── Subsystem health matrix, KPI metrics, quick actions
    ├── Copilot Chat (/chat) ─── Multi-session conversations, memory & tool indicators
    ├── Persistent Memory (/memory) ─── Semantic vector search, recall list, memory deletion
    ├── Agents & Discovery (/agents) ─── Untrusted vs. Trusted boundary, signed card probing
    ├── Tasks & Negotiation (/tasks) ─── Task delegation, multi-round negotiation timeline
    ├── Proactive Workflows (/workflows) ─── Step execution DAGs, human approval banners
    ├── MCP Tools (/tools) ─── Built-in tool registry, parameters schema, interactive runner
    ├── Permissions (/permissions) ─── Policy hierarchy, active consents, policy simulator
    ├── Identity (/identity) ─── Ed25519 public key, DID string, card export, signature verifier
    └── Activity (/activity) ─── Unified cryptographic audit stream (policy, tools, A2A)
    │
    ▼ (REST API / CORS)
Nexus Backend (http://localhost:8000)
```

### Running the Frontend

```bash
# 1. Start the FastAPI backend
cd nexus
python run.py
# Backend runs at http://localhost:8000

# 2. In another terminal, start the Next.js frontend
cd nexus/frontend
npm run dev
# Frontend runs at http://localhost:3000
```

### Building for Production

```bash
cd nexus/frontend
npm run build
npm run start
```

---

## 12. What Part 10 implements: Controlled Autonomy & Decision Engine

Part 10 introduces **controlled autonomy** to the Nexus Personal AI Network. Nexus can observe triggers, plan bounded multi-step executions, and take initiative safely while strictly preserving human authority and cryptographic invariants.

```text
               Trigger Event / User Goal
                          ↓
                ActionPlanner (Bounded)
              (Explicit Allowed Actions)
                          ↓
         ┌─────────────────────────────────┐
         │     DETERMINISTIC DECISION      │
         │             ENGINE              │
         │  (13-Step Non-Bypassable Pipeline)│
         └─────────────────────────────────┘
             /          |          \         \
          ALLOW        ASK        DENY       STOP
           ↓            ↓          ↓          ↓
       Execution     Approval    Policy     Budget
      Coordinator     Pause     Violation    Halt
           ↓            ↓
   (Tools/A2A/Workflows)  Human-in-the-Loop
```

### Core Security Invariants

1. **LLM is NEVER the Security Authority**: While an LLM or planner can propose actions, every single action must pass through the deterministic `DecisionEngine`, `PolicyService`, `ConsentService`, and `IdentityService`.
2. **Deterministic Risk Classifier**: Actions are classified into `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL` risk via deterministic rule matching, independent of LLM prompts.
3. **Hard Budget & Resource Limits**: Unconditional hard limits on `max_steps_per_run`, `max_tool_calls_per_run`, `max_remote_tasks_per_run`, and `max_runtime_seconds` that trigger a deterministic `STOP`.
4. **Human-in-the-Loop Approvals**: When an action requires confirmation (`ASK`), the run pauses in `waiting_approval` status until the owner explicitly approves or rejects it.
5. **Tamper-Evident Audit Trail**: Every evaluated decision, risk score, policy gate result, and owner approval is durably logged in PostgreSQL with owner isolation.
6. **Crash Recovery & Reconciliation**: Interrupted runs left in `RUNNING` status during a server crash are safely reconciled on startup (`reconcile_on_startup()`) preventing orphaned executions.

### Operating Modes

| Mode | Behavior | Security Posture |
| --- | --- | --- |
| `off` | Autonomy disabled | All autonomous runs are rejected or stopped immediately |
| `assisted` | Human approval required | Owner must approve every external or state-changing action |
| `bounded` (Default) | Safe autonomy | Low-risk local steps run autonomously; high-risk / external require approval |
| `fully_delegated` | Autonomous within policy | Policy bounds strictly enforced; minimal approval interruptions |

### REST API Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/autonomy/config` | Retrieve owner autonomy configuration and modes |
| `PATCH` | `/autonomy/config` | Update autonomy mode, step limits, and approval policies |
| `POST` | `/autonomy/runs` | Submit an autonomous goal or custom bounded action plan |
| `GET` | `/autonomy/runs` | List owner's autonomous runs with optional status filter |
| `GET` | `/autonomy/runs/{run_id}` | Inspect run status, current step, context, and plan |
| `POST` | `/autonomy/runs/{run_id}/execute` | Advance/execute the next step of an active run |
| `POST` | `/autonomy/runs/{run_id}/approve` | Owner grants approval to a run paused in `waiting_approval` |
| `POST` | `/autonomy/runs/{run_id}/reject` | Owner rejects pending approval, aborting the run |
| `POST` | `/autonomy/runs/{run_id}/cancel` | Cancel an active run safely |
| `GET` | `/autonomy/runs/{run_id}/decisions` | Query deterministic decisions and risk evaluations |
| `GET` | `/autonomy/audits` | Query durable audit trigger history |
| `POST` | `/autonomy/evaluate` | Dry-run evaluate an action against the Decision Engine |

### Verification & Testing

```bash
# Run Part 10 comprehensive autonomy test suite
python -m pytest tests/test_part10_autonomy.py -v

# Run full project regression suite across Parts 1–10 (414 tests)
python -m pytest -q

# Run end-to-end standalone demo showcasing all 5 autonomy scenarios
python demo_part10_autonomy.py
```

---

**Parts 1–10 and Frontend complete.**

