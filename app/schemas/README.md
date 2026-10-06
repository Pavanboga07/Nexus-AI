# Schema layering contract

Nexus has **two** schema homes. They are not duplicates — they answer
different questions. Edit the wrong one and the frontend contract test
(`tests/test_m10_frontend_contract.py`) will tell you.

## 1. Wire schemas — `app/<domain>/schemas.py`

The **protocol truth**: what crosses a trust boundary.

- `app/a2a/schemas.py` — A2A envelopes, task payloads, capability refs.
  Signed, canonicalized, versioned (`PROTOCOL_VERSION`). Changing a field
  here is a **protocol change**: it affects signature bytes, peer
  compatibility, and the conformance vectors in `tests/vectors/`.
- Other `app/<domain>/schemas.py` files hold the domain's core shapes
  (e.g. `app/autonomy/schemas.py`).

**Edit here when:** the wire format, the signed bytes, or cross-agent
compatibility changes.

## 2. API schemas — `app/schemas/*.py`

The **HTTP truth**: what the REST API accepts and returns.

- `app/schemas/a2a.py`, `chat.py`, `workflows.py`, … — request/response
  models for FastAPI routes. They may *import from* the wire schemas
  (e.g. `app/schemas/a2a.py` re-uses patterns from `app/a2a/schemas.py`)
  but they never change what goes on the wire.
- Validation that is HTTP-specific (query params, pagination, error
  envelopes) lives here.

**Edit here when:** an endpoint's request/response shape changes and the
protocol itself does not.

## Rules

1. **Wire schemas never import from `app/schemas/`** — the dependency
   arrow points one way (`app/schemas/*` -> `app/<domain>/schemas.py`).
   The protocol layer must not know about the HTTP layer.
2. **One concept, one canonical definition.** If a shape exists on the
   wire, the API schema imports or references it; it does not redefine
   it with slightly different field names.
3. **The contract test is the backstop.** `test_m10_frontend_contract.py`
   statically checks that API paths, field names, and link targets match
   between backend and frontend. If you rename a field, update the test's
   expectations — a green test with a stale expectation is worse than a
   red one.
