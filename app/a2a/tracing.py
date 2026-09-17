"""Propagating the request's trace id onto outbound A2A envelopes (M11).

The audit finding was that nothing could be traced from the user through the
API, the gateway, a peer agent, a tool, and the database, because no identifier
was carried. ``TraceContext`` already existed in the protocol (M6) and was
always ``None`` on the wire - so the field was documented and unused.

Two rules:

1. **The local trace id is preferred, not regenerated.** Joining an existing
   trace is the entire point; minting a new id at the boundary splits it in two.
2. **A non-conforming id is dropped, not coerced.** The protocol constrains
   ``trace_id`` to 16-32 lowercase hex. Our own ids are ``uuid4().hex`` (32 hex
   characters, so they always fit), but a *caller-supplied* id may be anything
   the sanitiser accepted - ``abc-123`` is legal for a log line and illegal in
   the envelope. Putting it on the wire would make every outbound message fail
   validation, which is a spectacular way to fix observability by breaking
   messaging. In that case the envelope simply carries no trace, and the local
   logs remain joinable by request.
"""

from __future__ import annotations

import re
import uuid

from app.a2a.schemas import TraceContext
from app.observability import current_trace_id, NO_TRACE

_HEX_ID = re.compile(r"^[0-9a-f]{16,32}$")


def trace_context_for_outbound() -> TraceContext | None:
    """The current trace, as the protocol requires it - or ``None``.

    Returns ``None`` when there is no trace in scope, so a message sent outside a
    request (a background job) carries no trace field rather than a placeholder
    that would look like a real id in someone's log search.
    """
    trace_id = current_trace_id()
    if trace_id in (NO_TRACE, ""):
        return None
    if not _HEX_ID.match(trace_id):
        # Accepted for logs, not representable in the protocol. Dropping it is
        # the only option that neither breaks the envelope nor lies about the id.
        return None
    return TraceContext(trace_id=trace_id, span_id=uuid.uuid4().hex[:16])


__all__ = ["trace_context_for_outbound"]
