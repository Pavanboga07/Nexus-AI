# Nexus Architecture — Dependency Rules

`app/main.py` is the **composition root**: it is the only place allowed to
wire subsystems together. Nothing imports `app.main`.

## Allowed dependency direction

Subsystems form rough layers. An arrow `A -> B` means "A may import B".
Importing **up** a layer (against the arrow) is a layering violation and
will usually surface as a circular import.

```
                    ┌──────────┐
                    │   api    │  transport only: routes reach services via
                    └────┬─────┘  the get_agent / app.state dependencies
                         │        never constructs domain objects
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│     a2a      │ │ orchestration│ │   autonomy   │
└──┬───────▲───┘ └──────┬───────┘ └──────┬───────┘
   │       │            │                │
   │   ┌───┴────────────┴────┐     ┌─────┴──────┐
   │   │      workflows      │     │   agent    │
   │   └─────────┬───────────┘     └─────┬──────┘
   │             │                       │
   ▼             ▼                       ▼
┌─────────────────────────────────────────────────┐
│  tools · policy · memory · identity · search    │  domain services
└──────────────────────┬──────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────┐
│   database · jobs · llm · config · observability │  infrastructure
└─────────────────────────────────────────────────┘
```

Measured 2026-10-06 (module-level imports only; `app/main.py` excluded as
the composition root):

- `api` -> a2a, agent, auth, autonomy, config, identity, llm, orchestration,
  policy, schemas, tools, workflows
- `workflows` -> a2a, autonomy, database, jobs, policy, schemas, tools
- `orchestration` -> a2a, autonomy, database, llm, policy
- `autonomy` -> database, policy, schemas, tools
- `agent` -> database, llm, memory, tools
- `a2a` -> config, database, identity, memory, policy, search
- `tools` -> config, database, policy, search
- `memory` -> database, llm
- `auth`, `identity`, `policy`, `jobs` -> database
- `search` -> a2a (reuses `validate_endpoint` + `A2AError`; one-way)
- `llm` -> config, jobs
- `schemas` -> a2a (`app/schemas/a2a.py` re-exports the wire schemas)
- `database` -> agent (`session_store` uses `agent.session` types only —
  never `agent.agent`)

## Rules

1. **Leaf modules stay leaves.** Types shared across subsystems live in a
   module that imports nothing from `app` — e.g. `RunStatus` lives in
   `app/autonomy/run_status.py`, not in `models.py`, so `app.workflows`
   can import it at top level. If you add a cross-subsystem type, put it
   in a leaf first.
2. **`workflows -> autonomy` is one-way.** Autonomy never imports
   workflows. The old method-level lazy imports in `workflows/service.py`
   were hoisted to top level once this was verified — do not reintroduce
   them.
3. **`a2a` never touches private keys.** Signing goes through
   `IdentityService`; verification is a pure function of the sender's
   published key.
4. **`ToolContext` is the only thing a tool may see** — no sessions, no
   secrets, no conversation history (see `app/tools/schemas.py`).
5. **Lazy imports are the exception, not the pattern.** A function-level
   import must carry a `# lazy: ...` comment naming the reason (a genuine
   circular import, or deferred heavy import). Unexplained lazy imports
   will be hoisted.
6. **Schema layering** is documented in `app/schemas/README.md` — wire
   schemas vs. API schemas, and which to edit.
