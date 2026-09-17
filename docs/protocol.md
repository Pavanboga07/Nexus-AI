# Nexus Agent Protocol

**Protocol:** `nexus-a2a` · **Current version:** `0.2` · **Supersedes:** `0.1` (accepted)

This document is normative. Where it and the implementation disagree, that is a
bug in the implementation — the conformance suite (`tests/test_m9_conformance.py`)
and the vectors in `tests/vectors/conformance.json` are the arbiter.

An implementation is **conformant** when it reproduces every canonical byte in
that vector file, verifies every signature in it, and is verifiable by the
vectors' recorded keys.

---

## 1. Identity

An agent's identity is derived entirely from an Ed25519 keypair. There is **no
registration authority and no shared secret**.

```
agent_id = "nexus:ed25519:" + hex( SHA-256( raw_public_key ) )[0:32]
```

- `raw_public_key` is the **32-byte raw** Ed25519 public key (not PEM, not DER).
- `[0:32]` is the first **16 bytes**, hex-encoded — 32 hex characters.
- The public key travels as base64 of those same raw 32 bytes.

Because the id is the fingerprint of the key, anyone holding the key can verify
the binding without asking anyone. An agent that changes its key changes its
`agent_id`; that is inherent, not a bug (see §7, key rotation).

### Recognition vs verification

A valid signature proves *who sent something*. It does **not** mean you should
accept it. Recognition is a separate decision: you must have **pinned** the
sender's public key. An unknown-but-validly-signed sender is rejected with
`UNTRUSTED_SENDER`.

---

## 2. Canonical serialization

Signatures cover canonical bytes, so both sides must produce byte-identical
output.

### 2.1 Rules

1. **UTF-8 JSON**, keys **sorted**, separators `,` and `:` with **no**
   whitespace, non-ASCII **not** escaped:
   ```python
   json.dumps(value, separators=(",", ":"), sort_keys=True,
              ensure_ascii=False).encode("utf-8")
   ```
2. **Floats are forbidden** anywhere in a signed structure. JSON float
   formatting is language- and platform-dependent (`0.1` may serialize as
   `0.10000000000000001`), which makes signatures non-portable. Use integers, or
   strings for decimals.
3. **The signature is excluded** from the bytes it covers.
4. **Absent ≠ null.** A field that is absent and a field that is present with
   value `null` canonicalize DIFFERENTLY. Conformant implementations therefore
   **omit** optional fields whose value is `None`, at every level of nesting.

Rule 4 is the one implementations get wrong. It is not cosmetic: before 0.2
there were no `capability`/`trace`/`authorization` fields. An implementation
that emitted them as `null` would change the signed bytes of every message and
silently break all previously-issued signatures.

### 2.2 Test vectors

`tests/vectors/conformance.json` → `canonical_json[]` gives input/output pairs.
Reproduce every `canonical_utf8_hex` exactly.

---

## 3. Envelope

Every A2A message is a signed envelope.

| Field | Required | Type | Notes |
|---|---|---|---|
| `protocol` | yes | `"nexus-a2a"` | |
| `version` | yes | `"0.1"` \| `"0.2"` | see §6 |
| `message_id` | yes | `[A-Za-z0-9_-]{1,128}` | unique per message; the replay key |
| `task_id` | yes | same | the unit of work being discussed |
| `sender` | yes | agent id | must equal the authenticated peer |
| `recipient` | yes | agent id | |
| `timestamp` | yes | `YYYY-MM-DDTHH:MM:SSZ` | strict UTC, second resolution |
| `expires_at` | yes | same | must be after `timestamp` |
| `message_type` | yes | see §4 | |
| `purpose` | yes | `[a-z0-9:_\-.]{1,64}` | policy dimensions key on this |
| `payload` | yes | object | no floats |
| `task_type` | no | slug | |
| `correlation_id` | no | id | 0.2 |
| `reply_to` | no | id | 0.2; the `message_id` being answered |
| `capability` | no | `{id, version}` | 0.2 |
| `authorization` | no | object | 0.2 |
| `trace` | no | `{trace_id, span_id?, parent_span_id?}` | 0.2 |
| `signature` | yes\* | base64 | \*required on the wire, excluded from signing |

`trace_id` is 16–32 lowercase hex characters.

### 3.1 Unknown fields

A receiver **MUST reject** an envelope containing an unknown field. Ignoring it
would mean the signer and receiver disagree about what was signed — precisely
the ambiguity an attacker wants. Reject with `UNKNOWN_FIELD`.

---

## 4. Message types

**0.1:** `request`, `response`, `task_request`, `task_response`, `task_proposal`

**0.2 adds:** `error`, `task_progress`, `task_cancel`, `task_cancelled`,
`capability_query`, `capability_response`, `approval_required`,
`approval_granted`, `approval_denied`

A 0.2-only type sent on a 0.1 envelope is a protocol violation.

### 4.1 Errors

Errors are structured messages, not HTTP statuses:

```json
{"message_type": "error",
 "payload": {"code": "POLICY_DENIED", "message": "human-readable"}}
```

Codes: `UNTRUSTED_SENDER`, `REVOKED_SENDER`, `IDENTITY_MISMATCH`,
`INVALID_SIGNATURE`, `EXPIRED`, `CLOCK_SKEW`, `REPLAY`, `RATE_LIMITED`,
`INVALID_ENVELOPE`, `NOT_ADDRESSED_TO_US`, `UNSUPPORTED_CAPABILITY`,
`UNSUPPORTED_VERSION`, `POLICY_DENIED`, `APPROVAL_REQUIRED`,
`NO_HANDLER`, `INTERNAL`.

---

## 5. Trust model

```
identity      the Ed25519 keypair; agent_id is its fingerprint
     ↓
authentication  a valid signature over canonical bytes
     ↓
recognition     the key is PINNED locally (trust)
     ↓
authorization   a policy decision about THIS action on THIS category
     ↓
action          the effect
```

**Authentication is not authorization.** A perfectly signed message from a
recognized agent is still evaluated against policy, and a `DENY` returns no
data. An implementation that treats "valid signature" as "allowed" is not
conformant.

### 5.1 What a receiver MUST verify, in order

1. size limit,
2. schema (required fields, patterns, no unknown fields),
3. `recipient` == our own `agent_id` (`NOT_ADDRESSED_TO_US`),
4. rate limit,
5. sender is pinned (`UNTRUSTED_SENDER`) and not revoked (`REVOKED_SENDER`),
6. the pinned key still hashes to the claimed `sender` (`IDENTITY_MISMATCH`),
7. signature over canonical bytes (`INVALID_SIGNATURE`),
8. time window: not expired (`EXPIRED`), timestamp not too far ahead
   (`CLOCK_SKEW`),
9. replay: this `message_id` has not been processed (`REPLAY`),
10. authorization.

Steps 5–9 apply to **responses too**. A response is as replayable as a request.

---

## 6. Versioning

`0.2` is **additive** over `0.1`. A `0.1` envelope has none of the 0.2 fields
and MUST remain valid.

- A receiver MUST accept every version it advertises.
- A sender MUST NOT put a 0.2-only field on a 0.1 envelope.
- Feature availability is *checked*, not assumed: `supports(feature)` is false
  for a 0.1 envelope.
- An unknown `version` is rejected (`UNSUPPORTED_VERSION`) — never silently
  treated as a supported one.

---

## 7. Capabilities

A capability is a typed, versioned contract, not a label.

```json
{"id": "calendar.availability",
 "version": "1.0",
 "description": "Check whether the owner is available at a time.",
 "data_category": "availability",
 "input_schema": {"type": "object",
                  "properties": {"date": {"type": "string"}},
                  "required": ["date"],
                  "additionalProperties": false},
 "output_schema": {"type": "object"}}
```

- `id`: dotted lowercase slug.
- `version`: `major` or `major.minor`.
- `data_category`: the **policy** category this exposes.

### 7.1 Version compatibility

A caller declaring `1.0` is served by a provider offering `1.7` (minor bumps are
additive) and refused by a provider offering `2.0`
(`UNSUPPORTED_CAPABILITY`). Compatibility is decided on the **major** component.

### 7.2 The declared `data_category` is authoritative

The category used for the authorization decision comes from the **capability
declaration**, not from the caller's payload. A caller must not be able to widen
disclosure by naming a different category than the capability advertises.

### 7.3 Schema subset

Supported keywords: `type`, `properties`, `required`, `items`, `enum`, `const`,
`additionalProperties`, `description`, `minLength`, `maxLength`, `minimum`,
`maximum`, `pattern`. Unknown keywords are **ignored**, so a richer third-party
schema still parses. Validation failures MUST name the offending path
(`payload.slots[1] must be ['integer']`), not just say "invalid".

---

## 8. Agent cards

Discovery is a signed self-describing document served at
`GET /.well-known/nexus-agent.json` (alias `GET /a2a/card`).

Required: `type` (`"agent-card"`), `protocol`, `version`, `agent_id`,
`display_name`, `public_key`, `endpoint`, `capabilities`,
`supported_purposes`, `issued_at`, `expires_at`, `signature`.

### 8.1 Verification — all four checks, in order

1. required fields present,
2. `agent_id` == fingerprint of `public_key`,
3. signature verifies under that key,
4. `issued_at` ≤ now < `expires_at`.

**A card is a hint until it verifies.** Do not populate your registry from
unsigned directory metadata: a relay that serves you a `public_key` alongside an
`agent_id` has proven nothing, because only the signature binds them.

---

## 9. Transport

### 9.1 Direct HTTP

`POST {endpoint}/a2a/messages`, body = the envelope JSON, response = the reply
envelope.

### 9.2 Nexus Gateway (preferred)

A WebSocket relay for agents behind NAT. Connect to `{gateway}/ws`:

1. gateway sends `auth_challenge` with 32 random bytes (base64),
2. sign those **raw challenge bytes** with your Ed25519 key,
3. send `auth_response` with `agent_id`, `public_key`, `signature` (base64),
4. gateway replies `auth_result`.

The gateway verifies that `agent_id` is the fingerprint of `public_key` **and**
that the signature verifies. Private keys never leave your process.

Frames afterwards: `relay_envelope`, `delivery`, `delivery_ack`, `heartbeat`,
`presence_query`. An envelope forwarded by the gateway is **not** re-signed and
not interpreted; end-to-end signatures remain authoritative, so a compromised
relay cannot forge a message.

---

## 10. Delivery semantics

- **At-least-once.** A sender may see a duplicate `message_id`; the receiver
  MUST deduplicate (§5.1 step 9) rather than process it twice.
- **Ack means delivered.** A gateway `delivery_ack` from the recipient is what
  confirms delivery; a relay writing bytes to a socket does not.
- **Offline buffering** exists at the gateway and honours `expires_at`.

---

## 11. Conformance checklist

- [ ] canonical bytes match every vector
- [ ] floats rejected in signed structures
- [ ] `None` omitted at every nesting level
- [ ] verifies a runtime-signed envelope and card
- [ ] its signature verifies in the runtime
- [ ] rejects unknown fields, replays, expired messages, untrusted senders
- [ ] accepts 0.1 and 0.2, rejects unknown versions
- [ ] a 0.2-only message type on a 0.1 envelope is refused
- [ ] capability versions compared by major component
- [ ] capability `data_category` drives authorization
- [ ] authentication never substitutes for authorization

---

## 12. Reference implementation

`nexus-sdk/` is a small, dependency-light Python implementation (Ed25519 via
`cryptography`, HTTP via `httpx`). It imports **nothing** from the Nexus
runtime, and the conformance suite enforces that by scanning its imports, so it
is a genuine independent implementation rather than a re-export.

```python
from nexus_sdk import AgentIdentity, NexusAgent

identity = AgentIdentity.generate()
agent = NexusAgent(
    identity=identity,
    display_name="Calendar bot",
    capabilities=[{"id": "calendar.availability", "version": "1.0",
                   "description": "Check availability",
                   "data_category": "availability",
                   "input_schema": {"type": "object"}}],
    on_request=lambda env: {"status": "completed", "available": True},
)
agent.trust(peer_agent_id, peer_public_key_b64)   # pin before sending
await agent.send(recipient_agent_id=peer_agent_id, endpoint="https://peer",
                 capability_id="calendar.availability", payload={})
```
