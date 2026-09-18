"""M10 regression tests: the UI's data contract.

The frontend audit finding was not cosmetic. The UI was written against API
shapes that do not exist:

  * ``TrustedAgent`` declared ``public_key_b64`` / ``endpoint_url`` /
    ``capabilities``; ``TrustedAgentOut`` returns ``endpoint`` and has no key or
    capability list, so every page reading those fields silently got
    ``undefined``.
  * the A2A audit was read as ``entries`` with ``sender_id`` / ``direction`` /
    ``timestamp``; the API returns ``messages`` with ``sender_agent_id`` /
    ``created_at``, so that audit section was always empty.
  * ``/a2a/agents/{id}/revoke`` was called as if it were a mutation endpoint
    shape that matched the declared type.
  * four unrelated endpoints were expected to answer "what needs approval?" and
    none of them actually did.

These tests assert against the RESPONSES the API really produces, so a change
to a response shape fails here rather than silently blanking a page.
"""

from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# Approvals: the Inbox's whole premise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_sources_exist_and_are_readable(db_a2a_client) -> None:
    """Every endpoint the Inbox fans out to must be reachable.

    The Inbox tolerates 503 ("not configured on this deployment") because a
    feature that is switched off must not hide approvals that DO exist. What it
    cannot tolerate is 404 or 500: those mean the fan-out points at the wrong
    route, and the page would present as an empty inbox - the worst possible
    failure for a page whose only job is to tell you something needs you.
    """
    for path, key in (
        ("/a2a/tasks", "tasks"),
        ("/workflows", "workflows"),
        ("/autonomy/runs", "runs"),
    ):
        response = await db_a2a_client.get(path)
        assert response.status_code in (200, 503), (
            f"{path} returned {response.status_code}. A 404/500 here means the "
            f"Inbox is pointing at a route that does not exist, and it would "
            f"silently show nothing. {response.text[:200]}"
        )
        if response.status_code == 503:
            # Unavailable is acceptable ONLY because the Inbox reports it.
            assert response.json()["error"]["code"] == "service_unavailable"
            continue
        body = response.json()
        assert key in body, (
            f"{path} does not return '{key}'; the frontend reads that key and "
            f"would render an empty list. Got: {sorted(body)}"
        )


@pytest.mark.asyncio
async def test_orchestration_runs_is_a_readable_approval_source(db_a2a_client) -> None:
    """The fourth approval source must be reachable, like the other three.

    Without the orchestrator attached this 503s with the shared envelope (which
    the Inbox reports as "not configured"); with it attached it returns the run
    list. A 404/500 would mean the fan-out points at the wrong route, and the
    orchestration source would silently show nothing.
    """
    response = await db_a2a_client.get("/orchestration/runs")
    assert response.status_code in (200, 503), (
        f"/orchestration/runs returned {response.status_code}. A 404/500 here "
        f"means the Inbox is pointing at a route that does not exist, and it "
        f"would silently show nothing. {response.text[:200]}"
    )
    if response.status_code == 503:
        # Unavailable is acceptable ONLY because the Inbox reports it.
        assert response.json()["error"]["code"] == "service_unavailable"
        return
    body = response.json()
    assert isinstance(body, list), (
        "GET /orchestration/runs returns a bare list "
        f"(response_model=list[OrchestrationRunResponse]), not an object. Got: "
        f"{sorted(body) if isinstance(body, dict) else type(body)}"
    )


@pytest.mark.asyncio
async def test_orchestration_pending_run_carries_the_fields_the_inbox_reads(
    db_orchestration_client, db_session_factory
) -> None:
    """A run waiting on the owner must expose run_id/state/goal over HTTP.

    The Inbox previously read `r.status`/`r.id`, neither of which
    OrchestrationRunResponse returns, so this source was permanently empty
    while looking implemented.
    """
    from app.database.repositories import OwnerRepository
    from app.orchestration.models import OrchestrationState
    from app.orchestration.repository import OrchestrationRunRepository

    async with db_session_factory() as session:
        owner = await OwnerRepository().get_or_create_default(session)
        run = await OrchestrationRunRepository().create(
            session,
            owner_id=owner.id,
            session_id=f"orch_{uuid.uuid4().hex[:8]}",
            goal="Ask Maya about Thursday",
            intent_type="ASK_PERSON",
            state=OrchestrationState.WAITING_APPROVAL.value,
            target_person="Maya",
        )
        run.approval_prompt = "Contact Maya on your behalf?"
        await session.commit()

    response = await db_orchestration_client.get("/orchestration/runs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list), (
        "GET /orchestration/runs must return a bare list "
        "(response_model=list[OrchestrationRunResponse]); the frontend reads it "
        f"via its Array.isArray fallback. Got: {type(body)}"
    )
    pending = [
        r
        for r in body
        if r.get("state") == OrchestrationState.WAITING_APPROVAL.value
    ]
    assert pending, (
        "the seeded WAITING_APPROVAL run is missing from /orchestration/runs; "
        "the Inbox would show nothing waiting. "
        f"Got states: {sorted({r.get('state') for r in body})}"
    )
    first = pending[0]
    for field in ("run_id", "state", "goal", "created_at"):
        assert field in first, (
            f"an orchestration run must carry {field!r}; the Inbox reads it. "
            f"Got: {sorted(first)}"
        )
    assert "status" not in first, (
        "OrchestrationRunResponse carries `state`, not `status`; the Inbox "
        "must read `state` or this source renders permanently empty."
    )


def test_orchestration_loader_maps_run_id_and_state_to_pending() -> None:
    """A backend run {run_id, state: WAITING_APPROVAL} must surface as pending.

    Static check (this repo has no frontend unit runner — see the plan): the
    loader must key items by `run_id` and filter on `state`.
    OrchestrationRunResponse has no `id`/`status`, which is why this source was
    permanently empty. The state comparison must be case-insensitive: the
    orchestration enum is UPPERCASE (`WAITING_APPROVAL`,
    app/orchestration/models.py) while the other three sources are lowercase.
    """
    source = (SRC / "lib" / "api" / "approvals.ts").read_text(encoding="utf-8")
    anchor = source.find("async function loadOrchestration")
    assert anchor != -1, "loadOrchestration is gone from approvals.ts"
    end = source.find("\n}\n", anchor)
    # Include the loader's dedicated pending predicate, which sits directly
    # above it: the state vocabulary lives there, not in the fan-out body.
    helper = source.find("function isOrchestrationPending")
    start = helper if 0 <= helper < anchor else anchor
    block = source[start : end if end != -1 else anchor + 2000]
    assert "run_id" in block, (
        "loadOrchestration must key items by `run_id`: "
        "OrchestrationRunResponse has no `id`."
    )
    assert ".state" in block, (
        "loadOrchestration must read `state`: OrchestrationRunResponse has no "
        "`status`, so filtering on it matches nothing, ever."
    )
    assert ".status" not in block, (
        "loadOrchestration still reads `status`, which OrchestrationRunResponse "
        "never returns — the orchestration source renders permanently empty."
    )
    assert "waiting_approval" in block.lower(), (
        "loadOrchestration must recognise the waiting-approval state as pending."
    )
    assert "toUpperCase" in block or "toLowerCase" in block, (
        "the orchestration state enum is UPPERCASE (WAITING_APPROVAL) while the "
        "other sources are lowercase — compare case-insensitively or the real "
        "backend states never match."
    )


def test_orchestration_loader_categorizes_waiting_and_done_buckets() -> None:
    """Non-pending orchestration states must land in waitingOutbox/recentlyDecided.

    B1's spec requires mapping non-pending states (WAITING_REMOTE/EXECUTING
    -> waiting-on-others; COMPLETED/FAILED/CANCELLED/EXPIRED -> terminal) so
    B5's outbox/decided panes have data to consume, but the loader only
    filtered pending. Waiting-on-others runs must land in `waitingOutbox`
    (not pending, not dropped) and terminal runs in `recentlyDecided`.
    State strings are taken from the backend enum, not guessed, and compared
    case-insensitively like the pending predicate.

    Static check (this repo has no frontend unit runner): every behavior
    below is asserted as guarded structure — state-set literal contents,
    predicate-to-set delegation, and guard-to-push adjacency — never as a
    bare "state string appears somewhere in the file".
    """
    import re

    from app.orchestration.models import OrchestrationState

    source = (SRC / "lib" / "api" / "approvals.ts").read_text(encoding="utf-8")

    # The extra buckets ride on the result object as optional fields, so the
    # pending `items` shape Inbox/Chat/AppShell consume today is untouched.
    anchor = source.find("export type ApprovalsResult")
    assert anchor != -1, "ApprovalsResult is gone from approvals.ts"
    result_block = source[anchor : anchor + 800]
    assert "waitingOutbox?" in result_block, (
        "ApprovalsResult must expose an optional `waitingOutbox` bucket so B5 "
        "has waiting-on-others runs without changing what Inbox reads."
    )
    assert "recentlyDecided?" in result_block, (
        "ApprovalsResult must expose an optional `recentlyDecided` bucket so B5 "
        "has terminal runs without changing what Inbox reads."
    )

    # The loader's buckets use the same names end to end (no waiting/done
    # aliases alongside waitingOutbox/recentlyDecided).
    buckets = re.search(
        r"export\s+type\s+OrchestrationBuckets\s*=\s*\{(.*?)\};",
        source,
        re.S,
    )
    assert buckets, "OrchestrationBuckets is gone from approvals.ts"
    bucket_body = buckets.group(1)
    assert re.search(r"\bpending\s*:\s*ApprovalItem\[\]", bucket_body), (
        "OrchestrationBuckets must keep the `pending` bucket the fan-out reads."
    )
    assert re.search(r"\bwaitingOutbox\s*:\s*ApprovalItem\[\]", bucket_body), (
        "OrchestrationBuckets must name the waiting bucket `waitingOutbox`, "
        "matching ApprovalsResult — a `waiting` alias splits the scheme."
    )
    assert re.search(r"\brecentlyDecided\s*:\s*ApprovalItem\[\]", bucket_body), (
        "OrchestrationBuckets must name the terminal bucket "
        "`recentlyDecided`, matching ApprovalsResult — a `done` alias splits "
        "the scheme."
    )
    assert not re.search(r"(?<![A-Za-z])waiting\s*:", bucket_body), (
        "OrchestrationBuckets still declares a `waiting` alias; use "
        "`waitingOutbox` everywhere."
    )
    assert not re.search(r"(?<![A-Za-z])done\s*:", bucket_body), (
        "OrchestrationBuckets still declares a `done` alias; use "
        "`recentlyDecided` everywhere."
    )

    # waitingOutbox state set holds exactly the non-terminal in-flight
    # states — verified against the enum, with no extras and none missing.
    # A bare substring check would pass if a state merely appeared in a
    # comment; matching the Set literal contents proves the loader reads it.
    m_waiting_states = re.search(
        r"ORCHESTRATION_WAITING_OUTBOX_STATES\s*=\s*new\s+Set\(\[(.*?)\]\)",
        source,
        re.S,
    )
    assert m_waiting_states, (
        "ORCHESTRATION_WAITING_OUTBOX_STATES set is gone from approvals.ts"
    )
    waiting_states = set(re.findall(r'"([A-Z_]+)"', m_waiting_states.group(1)))
    assert waiting_states == {
        OrchestrationState.WAITING_REMOTE.value,
        OrchestrationState.EXECUTING.value,
    }, (
        "the waitingOutbox set must hold exactly WAITING_REMOTE + EXECUTING "
        f"(app/orchestration/models.py OrchestrationState); got "
        f"{sorted(waiting_states)}"
    )

    # recentlyDecided set holds exactly the terminal states.
    m_decided_states = re.search(
        r"ORCHESTRATION_RECENTLY_DECIDED_STATES\s*=\s*new\s+Set\(\[(.*?)\]\)",
        source,
        re.S,
    )
    assert m_decided_states, (
        "ORCHESTRATION_RECENTLY_DECIDED_STATES set is gone from approvals.ts"
    )
    decided_states = set(re.findall(r'"([A-Z_]+)"', m_decided_states.group(1)))
    assert decided_states == {
        OrchestrationState.COMPLETED.value,
        OrchestrationState.FAILED.value,
        OrchestrationState.CANCELLED.value,
        OrchestrationState.EXPIRED.value,
    }, (
        "the recentlyDecided set must hold exactly the terminal states "
        f"(COMPLETED/FAILED/CANCELLED/EXPIRED); got {sorted(decided_states)}"
    )

    # Each predicate delegates to its own set (and only that set): this is
    # the link between the literals above and the branches below. A swapped
    # predicate would route every waiting run into the decided bucket.
    m_waiting_pred = re.search(
        r"function\s+isWaitingOutbox\([^)]*\)[^{]*\{(.*?)\n\}",
        source,
        re.S,
    )
    assert m_waiting_pred, "isWaitingOutbox predicate is gone from approvals.ts"
    assert "ORCHESTRATION_WAITING_OUTBOX_STATES" in m_waiting_pred.group(1), (
        "isWaitingOutbox must read ORCHESTRATION_WAITING_OUTBOX_STATES."
    )
    assert "ORCHESTRATION_RECENTLY_DECIDED_STATES" not in m_waiting_pred.group(
        1
    ), "isWaitingOutbox is cross-wired to the terminal set."
    assert (
        "toUpperCase" in m_waiting_pred.group(1)
        or "toLowerCase" in m_waiting_pred.group(1)
    ), (
        "orchestration states are UPPERCASE — isWaitingOutbox must compare "
        "case-insensitively like the pending predicate."
    )

    m_decided_pred = re.search(
        r"function\s+isRecentlyDecided\([^)]*\)[^{]*\{(.*?)\n\}",
        source,
        re.S,
    )
    assert m_decided_pred, (
        "isRecentlyDecided predicate is gone from approvals.ts"
    )
    assert "ORCHESTRATION_RECENTLY_DECIDED_STATES" in m_decided_pred.group(1), (
        "isRecentlyDecided must read ORCHESTRATION_RECENTLY_DECIDED_STATES."
    )
    assert "ORCHESTRATION_WAITING_OUTBOX_STATES" not in m_decided_pred.group(
        1
    ), "isRecentlyDecided is cross-wired to the waiting set."
    assert (
        "toUpperCase" in m_decided_pred.group(1)
        or "toLowerCase" in m_decided_pred.group(1)
    ), (
        "orchestration states are UPPERCASE — isRecentlyDecided must compare "
        "case-insensitively like the pending predicate."
    )

    # The old predicate names are gone: one scheme, not two.
    assert "isOrchestrationWaiting" not in source, (
        "isOrchestrationWaiting survives; the unified predicate is "
        "isWaitingOutbox."
    )
    assert "isOrchestrationTerminal" not in source, (
        "isOrchestrationTerminal survives; the unified predicate is "
        "isRecentlyDecided."
    )

    # Loader branches: each push call site sits inside its own guard. This is
    # the behavior under test — a waiting-state run lands in waitingOutbox, a
    # terminal run in recentlyDecided, never crossed and never pending. The
    # regexes span the guard and the push so a push moved under the wrong
    # branch (or into no branch) fails, where a substring check would pass.
    loader_at = source.find("async function loadOrchestration")
    assert loader_at != -1, "loadOrchestration is gone from approvals.ts"
    loader_end = source.find("\n}\n", loader_at)
    loader = source[
        loader_at : loader_end if loader_end != -1 else loader_at + 3000
    ]
    assert re.search(
        r"if\s*\(\s*isOrchestrationPending\s*\(\s*r\.state\s*\)\s*\)"
        r"\s*pending\.push\s*\(",
        loader,
    ), (
        "loadOrchestration must push into `pending` only under the "
        "isOrchestrationPending(r.state) guard."
    )
    assert re.search(
        r"else\s+if\s*\(\s*isWaitingOutbox\s*\(\s*r\.state\s*\)\s*\)"
        r"[^\n]*\n\s*waitingOutbox\.push\s*\(",
        loader,
    ), (
        "loadOrchestration must push into `waitingOutbox` only under the "
        "isWaitingOutbox(r.state) guard — the push call site must sit inside "
        "the waiting-states branch."
    )
    assert re.search(
        r"else\s+if\s*\(\s*isRecentlyDecided\s*\(\s*r\.state\s*\)\s*\)"
        r"[^\n]*\n\s*recentlyDecided\.push\s*\(",
        loader,
    ), (
        "loadOrchestration must push into `recentlyDecided` only under the "
        "isRecentlyDecided(r.state) guard — the push call site must sit "
        "inside the terminal-states branch."
    )
    # Old bucket pushes are gone from the loader.
    assert "waiting.push" not in loader, (
        "loadOrchestration still pushes into a `waiting` alias; the unified "
        "bucket is `waitingOutbox`."
    )
    assert "done.push" not in loader, (
        "loadOrchestration still pushes into a `done` alias; the unified "
        "bucket is `recentlyDecided`."
    )
    # The deliberate silent drop is documented where it happens, naming the
    # ignored intermediate states and pointing at B5 (see approvals.ts loop).
    for dropped in (
        OrchestrationState.UNDERSTANDING.value,
        OrchestrationState.PLANNING.value,
        OrchestrationState.AUTHORIZING.value,
        OrchestrationState.PROCESSING_RESULT.value,
    ):
        assert dropped in loader, (
            f"the silent-drop comment must name ignored {dropped!r} "
            "(app/orchestration/models.py OrchestrationState) so a reader "
            "knows the fall-through is deliberate."
        )
    assert "B5" in loader, (
        "the silent-drop comment must point at B5, which owns whether any "
        "ignored intermediate state ever needs a surface."
    )

    # listApprovals must actually populate both buckets under the same names
    # (declaring the fields without filling them leaves B5 with nothing to
    # consume). Assert the wiring, not the mere presence of the words.
    fanout = source.find("export async function listApprovals")
    assert fanout != -1, "listApprovals is gone from approvals.ts"
    fanout_block = source[fanout : fanout + 4000]
    assert re.search(
        r"waitingOutbox\s*=\s*orchestration\.items\.waitingOutbox",
        fanout_block,
    ), (
        "listApprovals must assign orchestration.items.waitingOutbox into "
        "`waitingOutbox`; declaring the field without filling it leaves B5 "
        "with nothing to consume."
    )
    assert re.search(
        r"recentlyDecided\s*=\s*orchestration\.items\.recentlyDecided",
        fanout_block,
    ), (
        "listApprovals must assign orchestration.items.recentlyDecided into "
        "`recentlyDecided`; declaring the field without filling it leaves B5 "
        "with nothing to consume."
    )
    assert not re.search(
        r"orchestration\.items\.waiting(?![A-Za-z])", fanout_block
    ), (
        "listApprovals still reads the old orchestration.items.waiting alias; "
        "the unified field is waitingOutbox."
    )
    assert not re.search(
        r"orchestration\.items\.done(?![A-Za-z])", fanout_block
    ), (
        "listApprovals still reads the old orchestration.items.done alias; "
        "the unified field is recentlyDecided."
    )


@pytest.mark.asyncio
async def test_approvals_use_the_statuses_the_inbox_filters_on(
    db_session_factory, db_owner_id
) -> None:
    """A pending approval must carry one of the statuses the Inbox recognises.

    The Inbox filters on `pending_approval` / `waiting_approval` /
    `approval_required`. If a subsystem renamed its pending state, the item
    would be invisible - silently, and only for that subsystem.
    """
    from app.a2a.models import A2ATask, TaskStatus

    async with db_session_factory() as session:
        task = A2ATask(
            owner_id=db_owner_id,
            task_id=f"task_{uuid.uuid4().hex[:8]}",
            sender_agent_id="nexus:ed25519:" + "a" * 32,
            recipient_agent_id="nexus:ed25519:" + "b" * 32,
            status=TaskStatus.PENDING_APPROVAL.value,
            task_type="availability_check",
            purpose="scheduling",
            request_payload={"data_category": "availability"},
        )
        session.add(task)
        await session.commit()

    # The three statuses the Inbox recognises, taken from the enums so a rename
    # in the backend shows up here rather than as an empty inbox.
    recognised = {"pending_approval", "waiting_approval", "approval_required"}
    assert TaskStatus.PENDING_APPROVAL.value in recognised
    from app.autonomy.models import RunStatus
    from app.workflows.models import WorkflowStatus

    assert RunStatus.WAITING_APPROVAL.value in recognised, (
        f"RunStatus.WAITING_APPROVAL is {RunStatus.WAITING_APPROVAL.value!r}, "
        "which the Inbox does not filter on"
    )
    assert WorkflowStatus.WAITING_APPROVAL.value in recognised, (
        f"WorkflowStatus.WAITING_APPROVAL is "
        f"{WorkflowStatus.WAITING_APPROVAL.value!r}, which the Inbox does not "
        "filter on"
    )


def _real_agent(label: str) -> dict[str, str]:
    """A real Ed25519 identity, so registration passes key verification.

    Registration verifies that agent_id is the fingerprint of public_key, so a
    made-up id is rejected with a 409 - which would test the rejection path
    rather than the shape this test is about.
    """
    import base64

    from app.identity import crypto

    _, public = crypto.generate_keypair()
    raw = crypto.public_key_bytes(public)
    return {
        "agent_id": crypto.agent_id_from_public_key(raw),
        "public_key": base64.b64encode(raw).decode("ascii"),
        "display_name": label,
        "endpoint": "https://peer.example/a2a",
    }


# ---------------------------------------------------------------------------
# Trusted agents: the People page's contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trusted_agent_shape_matches_what_the_ui_reads(db_a2a_client) -> None:
    """The response must carry `endpoint`, and must NOT be expected to carry a
    public key or capability list.

    The frontend previously typed `endpoint_url`, `public_key_b64` and
    `capabilities`. A page reading `agent.endpoint_url` renders blank with no
    error, which is why this is asserted rather than assumed.
    """
    response = await db_a2a_client.get("/a2a/agents")
    assert response.status_code == 200
    body = response.json()
    assert "agents" in body and "total" in body

    created = await db_a2a_client.post("/a2a/agents", json=_real_agent("Shape check"))
    assert created.status_code == 201, created.text
    record = created.json()

    assert "endpoint" in record, (
        "TrustedAgentOut must expose `endpoint`; the UI reads that field"
    )
    assert record["endpoint"] == "https://peer.example/a2a"
    # These are the fields the old frontend type invented. Their absence is the
    # point: a type that declares them makes pages read `undefined` happily.
    for absent in ("endpoint_url", "public_key_b64", "capabilities"):
        assert absent not in record, (
            f"TrustedAgentOut unexpectedly has {absent!r}; if it is now real, "
            "update the frontend type instead of leaving it assumed"
        )
    assert set(record) >= {"agent_id", "display_name", "endpoint", "status"}


@pytest.mark.asyncio
async def test_revoke_is_a_post_and_keeps_the_record(db_a2a_client) -> None:
    """Revocation is POST (auditable); DELETE removes. The UI must use POST.

    Calling the wrong method here yields a 405, which the People page would
    surface as a generic failure with no explanation of what went wrong.
    """
    peer = _real_agent("Revoke check")
    created = await db_a2a_client.post("/a2a/agents", json=peer)
    assert created.status_code == 201, created.text

    revoked = await db_a2a_client.post(
        f"/a2a/agents/{peer['agent_id']}/revoke"
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"

    # The record survives revocation, which is why DELETE is a separate action.
    listed = await db_a2a_client.get("/a2a/agents")
    assert any(a["agent_id"] == peer["agent_id"] for a in listed.json()["agents"])

    removed = await db_a2a_client.delete(f"/a2a/agents/{peer['agent_id']}")
    assert removed.status_code == 200
    assert removed.json()["deleted"] is True


# ---------------------------------------------------------------------------
# Audit trail: the Activity page's contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_audit_returns_messages_with_the_documented_fields(
    db_a2a_client,
) -> None:
    """The audit returns `messages`, not `entries`.

    The Activity page read `entries`, so its A2A section was permanently empty
    while looking implemented.
    """
    response = await db_a2a_client.get("/a2a/audit/list")
    assert response.status_code == 200
    body = response.json()
    assert "messages" in body, (
        f"the A2A audit must return 'messages'; got {sorted(body)}. "
        "The frontend reads that key."
    )
    assert "entries" not in body
    assert body["total"] == len(body["messages"])


@pytest.mark.asyncio
async def test_directory_search_is_reachable_or_explicitly_unavailable(
    db_a2a_client,
) -> None:
    """The People page's search must not 404.

    A 503 with the shared envelope is acceptable (no gateway configured on this
    deployment). A 404 would mean the route name is wrong, and the page would
    then show "no matches" instead of "search is unavailable" - a difference
    that matters, because one tells the user to try a different name and the
    other tells them the feature is off.
    """
    response = await db_a2a_client.get("/a2a/directory/search?q=nobody")
    assert response.status_code in (200, 503), response.text
    if response.status_code == 200:
        body = response.json()
        assert "agents" in body and "total" in body
    else:
        assert response.json()["error"]["code"] == "service_unavailable"


# ---------------------------------------------------------------------------
# Error envelope: what every page's error handling depends on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_errors_use_the_envelope_the_client_parses(db_a2a_client) -> None:
    """The client reads `error.message`. A bare `detail` renders as "HTTP 400"."""
    response = await db_a2a_client.post(
        "/a2a/agents",
        json={
            "agent_id": "nexus:ed25519:" + "a" * 32,
            # deliberately mismatched key so the API rejects it
            "public_key": "bm90LWEtcmVhbC1rZXk=",
            "display_name": "Mismatch",
            "endpoint": "https://peer.example/a2a",
        },
    )
    assert response.status_code >= 400
    body = response.json()
    assert "error" in body, body
    assert isinstance(body["error"], dict), (
        "the error envelope must be an object with code/message; a bare string "
        "means the client cannot show the reason: " + str(body)
    )
    assert "message" in body["error"]
    assert "code" in body["error"]


# ---------------------------------------------------------------------------
# Public vs authenticated surfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_health_is_minimal_and_status_is_authenticated(
    db_client,
) -> None:
    """The UI reads /health for liveness and /system/status for detail.

    /health must stay minimal (it is public), and /system/status must exist for
    the Agent page's status grid.
    """
    health = await db_client.get("/health")
    assert health.status_code == 200
    assert set(health.json()) == {"status", "version"}

    status = await db_client.get("/system/status")
    assert status.status_code == 200
    body = status.json()
    for field in ("llm_provider", "llm_configured", "memory", "identity", "a2a"):
        assert field in body, f"the Agent page reads {field!r} from /system/status"


# ---------------------------------------------------------------------------
# Route integrity: links and calls that point at nothing
# ---------------------------------------------------------------------------
#
# These are cheap static checks over the frontend source, and they exist because
# this class of bug is invisible at runtime. A Next.js page that links to a route
# that does not exist renders a perfectly normal-looking link, and the 404 only
# appears when a person clicks it. `apiFetch("/workflows")` against a renamed
# route shows an error card on one page and nothing anywhere else.
#
# The specific regressions these lock down:
#   * `/settings` was a hub whose links pointed at `/agents` and `/identity`,
#     both deleted during the redesign - 3 of its 9 cards 404'd.
#   * the Agent page linked to `/agent/memory` while the page actually lived at
#     `/memory`, so "Memory" was a 404 while looking like the one working link.
#   * the detail pages lived outside the `(app)` route group, so they rendered
#     with no sidebar and no way back except a link to the redirect at `/`.


import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
SRC = FRONTEND / "src"
APP_DIR = SRC / "app"
API_DIR = SRC / "lib" / "api"


def _strip_comments(source: str) -> str:
    """Blank out comments, keeping every other character in place.

    String literals are deliberately KEPT: the thing under test is `href="/x"`,
    so blanking quoted text would erase the evidence. Comments are stripped
    because this codebase comments heavily, and prose mentioning a link that was
    removed would otherwise fail the check that removed it.

    Hand-written rather than `tokenize`: these are TypeScript files, and
    Python's tokenizer does not understand them.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        two = source[i : i + 2]
        if two == "//":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
        elif two == "/*":
            while i < n and source[i : i + 2] != "*/":
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
        elif ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                if source[i] == "\\":
                    out.append(source[i : i + 2])
                    i += 2
                    continue
                out.append(source[i])
                if source[i] == quote:
                    i += 1
                    break
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _page_routes() -> set[str]:
    """Every URL a `page.tsx` answers, with route groups stripped.

    `(app)` is a Next.js route group: it organises files without appearing in
    the URL, which is exactly why it is easy to get wrong by hand.
    """
    routes: set[str] = set()
    for page in APP_DIR.rglob("page.tsx"):
        rel = page.parent.relative_to(APP_DIR)
        segments = [s for s in rel.parts if not (s.startswith("(") and s.endswith(")"))]
        routes.add("/" + "/".join(segments) if segments else "/")
    return routes


def _matches_route(href: str, routes: set[str]) -> bool:
    """True if `href` resolves to a page, treating a dynamic segment as a hole."""
    path = href.split("?")[0].split("#")[0].rstrip("/") or "/"
    if path in routes:
        return True
    parts = path.strip("/").split("/") if path != "/" else []
    for route in routes:
        cand = route.strip("/").split("/") if route != "/" else []
        if len(cand) != len(parts):
            continue
        if all(
            c == p or c.startswith("[") or p == "" or c == "*"
            for c, p in zip(cand, parts)
        ):
            return True
    return False


def _code_only(path: Path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def test_static_links_point_at_real_pages() -> None:
    """Every literal `href`/`router.push` in the UI must resolve.

    Only literal strings are checked. A computed href (`/agent/${name}`) is not
    a link the compiler or this test can verify, so it is skipped rather than
    guessed at.
    """
    routes = _page_routes()
    assert routes, "no page.tsx files found - the app directory moved"
    assert "/inbox" in routes, f"the sidebar's main destination is missing: {routes}"

    offenders: list[str] = []
    pattern = re.compile(
        r"""(?:href\s*=\s*|router\.(?:push|replace)\(\s*)["'](/[^"'\s]*)["']"""
    )
    for path in APP_DIR.rglob("*.tsx"):
        if path.name == "layout.tsx" and "(app)" not in str(path):
            continue
        for match in pattern.finditer(_code_only(path)):
            href = match.group(1)
            if not _matches_route(href, routes):
                offenders.append(f"{path.relative_to(FRONTEND)} -> {href}")

    assert not offenders, (
        "these links point at routes with no page.tsx, so they 404 on click:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_detail_pages_are_inside_the_app_shell() -> None:
    """A page outside the `(app)` group renders without the sidebar.

    The shell is applied by `src/app/(app)/layout.tsx`, so anything at
    `src/app/<name>/page.tsx` is a dead end: no navigation, and its only escape
    is whatever back-link the page happens to draw. Autonomy shipped this way.
    """
    strays = []
    for page in APP_DIR.rglob("page.tsx"):
        if APP_DIR / "(app)" in page.parents:
            continue
        if page.parent == APP_DIR:
            continue  # "/" - a redirect, deliberately shell-less
        if page.parent.name == "login":
            continue  # must not render the shell (see the login contract)
        strays.append(str(page.relative_to(FRONTEND)))

    assert not strays, (
        "these pages sit outside the (app) route group, so they render with no "
        "sidebar and no navigation:\n  " + "\n  ".join(strays)
    )


def test_every_detail_page_is_reachable_from_the_agent_hub() -> None:
    """The sidebar shows four destinations; the rest must be linked from /agent.

    If a screen is not in `NAV` and not linked from the Agent page, it has no
    inbound link at all - reachable only by typing the URL.
    """
    shell = (SRC / "components" / "layout" / "AppShell.tsx").read_text(encoding="utf-8")
    in_nav = set(re.findall(r'href:\s*"([^"]+)"', shell))
    assert in_nav, "could not read the sidebar's destinations"

    hub = (APP_DIR / "(app)" / "agent" / "page.tsx").read_text(encoding="utf-8")
    hub_links = set(re.findall(r'href:\s*"([^"]+)"', hub))

    linked = in_nav | hub_links | {"/login", "/"}
    unreachable = sorted(
        route
        for route in _page_routes()
        if route not in linked and not route.startswith("/agent/")
    )
    # /agent/* are reached through the hub's own links, checked just below.
    for route in sorted(r for r in _page_routes() if r.startswith("/agent/")):
        assert route in hub_links, (
            f"{route} exists but the Agent page does not link to it, so it is "
            f"unreachable from the UI. Add it to the hub's list."
        )
    assert not unreachable, (
        "these pages have no inbound link (not in the sidebar, not on /agent):\n  "
        + "\n  ".join(unreachable)
    )


def test_api_client_paths_have_matching_backend_routes() -> None:
    """Every literal path in `lib/api/*.ts` must be mounted by the app.

    The audit found the frontend written against routes that did not exist. A
    typed wrapper around a nonexistent path compiles, type-checks, and fails
    only when a page calls it.
    """
    from app.main import create_app

    routes = set()
    for route in create_app().routes:
        path = getattr(route, "path", None)
        if path:
            routes.add(path.rstrip("/") or "/")

    prefixes = set()
    for route in routes:
        prefixes.add("/" + route.strip("/").split("/")[0])

    offenders: list[str] = []
    call = re.compile(r"""apiFetch<[^>]*>\(\s*[`"'](/[^`"'$\s]*)""")
    for path in API_DIR.glob("*.ts"):
        for match in call.finditer(_code_only(path)):
            endpoint = match.group(1)
            if endpoint in routes:
                continue
            if "/" + endpoint.strip("/").split("/")[0] in prefixes:
                continue  # a concrete path under a real prefix
            offenders.append(f"{path.name} -> {endpoint}")

    assert not offenders, (
        "these API calls have no matching backend route (the wrapper would "
        "compile and fail at runtime):\n  " + "\n  ".join(sorted(set(offenders)))
    )


# ---------------------------------------------------------------------------
# Declared response shapes vs. what the pages actually read
# ---------------------------------------------------------------------------
#
# The audit's single most expensive frontend finding was a family, not a bug:
# a page reads `value.audits`, the wrapper declares `{decisions}`, and because
# the value reaching the page is `any`, TypeScript says nothing. The list just
# renders empty, forever, with no error anywhere.
#
# These two checks close the family. They are static, so they need no browser.

#: Field reads that are legitimately absent from a declared return type because
#: they are not response fields at all. Kept explicit and small: every entry is a
#: decision that a human made, not a place to silence the check.
_READ_WHITELIST = {
    "length",
    "map",
    "filter",
    "forEach",
    "then",
    "catch",
    "status",
    "toString",
    "toFixed",
    "toLowerCase",
    "toUpperCase",
    "includes",
    "slice",
    "split",
    "join",
    "trim",
    "push",
    "some",
    "every",
    "find",
    "reduce",
    "sort",
    "concat",
    "indexOf",
    "keys",
    "values",
    "entries",
    "json",
    "ok",
}


def _declared_return_fields(source: str, fn: str) -> set[str] | None:
    """Field names in the return type of `export async function fn`.

    Reads the `Promise<{...}>` annotation literally. Returns None when the
    function has no such annotation (so a wrapper that returns a named type is
    skipped rather than guessed at).
    """
    anchor = re.search(
        rf"export\s+async\s+function\s+{re.escape(fn)}\b", source
    )
    if not anchor:
        return None
    body = source[anchor.end() : anchor.end() + 1200]
    brace = body.find("{")
    if brace == -1:
        return None
    depth = 0
    end = None
    for i in range(brace, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        return None
    literal = body[brace + 1 : end]
    return set(re.findall(r"([A-Za-z_]\w*)\s*[?]?\s*:", literal))


def test_api_wrappers_declare_the_fields_pages_read() -> None:
    """A page reading `value.<field>` must have that field in the wrapper.

    The page's local aliases are resolved first (`const res = await listX()`),
    which is the form the real bugs took: `auditRes.value.entries` against a
    wrapper returning `{executions}`, and `polRes.value.audits` against
    `{decisions}`.
    """
    api_source = {p.name: _code_only(p) for p in API_DIR.glob("*.ts")}
    fn_fields: dict[str, set[str] | None] = {}
    local_names: dict[str, str] = {}

    exported = re.compile(r"export\s+async\s+function\s+(\w+)")
    for text in api_source.values():
        for match in exported.finditer(text):
            fn_fields[match.group(1)] = _declared_return_fields(text, match.group(1))

    offenders: list[str] = []
    alias = re.compile(r"const\s+(\w+)\s*=\s*await\s+(\w+)\s*\(")
    read = re.compile(r"\b(\w+)\.value\.(\w+)")

    for page in list(APP_DIR.rglob("*.tsx")) + list(APP_DIR.rglob("*.ts")):
        text = _code_only(page)
        source = page.read_text(encoding="utf-8")
        imported = {
            name
            for name in fn_fields
            if re.search(rf"\b{name}\b", source)
        }
        for match in alias.finditer(text):
            if match.group(2) in imported:
                local_names[match.group(1)] = match.group(2)

        for match in read.finditer(text):
            local, field = match.group(1), match.group(2)
            fn = local_names.get(local)
            if fn is None:
                continue
            declared = fn_fields.get(fn)
            if declared is None or field in declared or field in _READ_WHITELIST:
                continue
            offenders.append(
                f"{page.relative_to(FRONTEND)}: {local}.value.{field} "
                f"({fn} returns {sorted(declared)})"
            )

    assert not offenders, (
        "these page reads are not in the shape the API wrapper declares, so they "
        "silently produce undefined:\n  " + "\n  ".join(sorted(set(offenders)))
    )


def test_shared_types_agree_with_the_backend_schema() -> None:
    """Every field on a shared frontend type must exist on the Pydantic model.

    Only the reverse direction is checked. A frontend type listing `category` and
    `source_type` on a memory - neither of which `MemoryOut` has - is a bug with
    no runtime symptom beyond a blank cell; a frontend type being a subset of the
    response is a choice (some screens genuinely need three fields), so it is not
    flagged.
    """
    import typing

    from pydantic import BaseModel

    from app.autonomy.schemas import (
        AutonomyApprovalOut,
        AutonomyConfigOut,
        AutonomyDecisionOut,
        AutonomyRunOut,
    )
    from app.schemas.memory import MemoryOut, MemorySearchResultOut
    from app.schemas.policy import ConsentOut, PolicyOut
    from app.schemas.tasks import TaskOut
    from app.schemas.tools import ToolMetadataOut
    from app.schemas.a2a import TrustedAgentOut
    from app.schemas.workflows import WorkflowOut, WorkflowStepOut

    pairs = {
        "Memory": MemoryOut,
        "MemorySearchResult": MemorySearchResultOut,
        "PolicyRule": PolicyOut,
        "Consent": ConsentOut,
        "A2ATask": TaskOut,
        "ToolInfo": ToolMetadataOut,
        "TrustedAgent": TrustedAgentOut,
        "WorkflowOut": WorkflowOut,
        "WorkflowStepOut": WorkflowStepOut,
        "AutonomyConfigOut": AutonomyConfigOut,
        "AutonomyDecisionOut": AutonomyDecisionOut,
        "AutonomyApprovalOut": AutonomyApprovalOut,
        "AutonomyRunOut": AutonomyRunOut,
    }

    source = (SRC / "types" / "api.ts").read_text(encoding="utf-8")
    source += (
        SRC / "lib" / "api" / "a2a.ts"
    ).read_text(encoding="utf-8")

    offenders: list[str] = []
    for name, model in pairs.items():
        block = re.search(
            rf"export\s+type\s+{name}\s*=\s*\{{(.*?)\n\}};", source, re.S
        )
        if block is None:
            offenders.append(f"{name}: type not found in the frontend")
            continue
        declared = set(
            re.findall(r"^  ([A-Za-z_]\w*)\s*[?]?\s*:", block.group(1), re.M)
        )
        schema = set(model.model_fields)
        extra = sorted(declared - schema)
        if extra:
            offenders.append(
                f"{name} declares {extra}, which "
                f"{model.__module__}.{model.__name__} does not return "
                f"(it returns {sorted(schema)})"
            )

    assert not offenders, (
        "these frontend types describe fields the API never sends, so every read "
        "of them yields undefined:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Capability map: the pickers' delegate vocabulary vs the backend registry
# ---------------------------------------------------------------------------
#
# The Tasks delegate modal and the Chat ask modal offer the live capability
# registry, but the delegate endpoint only accepts registered task-handler
# names — two separate backend vocabularies paired by
# A2AService._register_default_capabilities, mirrored on the frontend by
# CAPABILITY_TASK_TYPES. A capability id renamed on the backend without
# updating the map sends a request the backend 400s; a registry id missing
# from the map renders with submit disabled forever.

#: Registry ids that deliberately have no delegate task_type. Kept explicit
#: and empty: every entry is a decision that a human made, not a place to
#: silence the check.
_INTENTIONALLY_UNMAPPED_CAPABILITIES: frozenset[str] = frozenset()


def _frontend_capability_task_types() -> dict[str, str]:
    """The CAPABILITY_TASK_TYPES literal in tasks.ts, keyed by capability id."""
    source = (SRC / "lib" / "api" / "tasks.ts").read_text(encoding="utf-8")
    match = re.search(r"CAPABILITY_TASK_TYPES[^=]*=\s*\{(.*?)\};", source, re.S)
    assert match, "CAPABILITY_TASK_TYPES is gone from tasks.ts"
    return dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', match.group(1)))


def _backend_capability_ids() -> set[str]:
    """The ids A2AService._register_default_capabilities actually declares.

    Built by running the real registration on a bare instance: the method
    only touches `self._capabilities`, so no service collaborators are needed
    and a backend rename shows up here rather than as a doomed request.
    """
    from app.a2a.service import A2AService

    service = A2AService.__new__(A2AService)
    service._capabilities = {}
    service._register_default_capabilities()
    return set(service._capabilities)


def test_capability_task_types_match_the_backend_registry() -> None:
    """Every frontend map key must be a real registry id, and vice versa.

    Static check (this repo has no frontend unit runner — see the plan): the
    map literal is read with a regex over its braces, so a key moved into a
    comment does not count as mapped.
    """
    frontend = _frontend_capability_task_types()
    assert frontend, (
        "CAPABILITY_TASK_TYPES has no entries; the pickers cannot send anything"
    )
    registry = _backend_capability_ids()
    assert registry, "the backend registers no capabilities"

    unknown = sorted(set(frontend) - registry)
    assert not unknown, (
        "these CAPABILITY_TASK_TYPES keys are not in the backend capability "
        "registry (A2AService._register_default_capabilities), so picking them "
        "sends a task_type the backend never paired:\n  " + "\n  ".join(unknown)
    )

    unmapped = sorted(
        cid
        for cid in registry
        if cid not in frontend
        and cid not in _INTENTIONALLY_UNMAPPED_CAPABILITIES
    )
    assert not unmapped, (
        "these registry ids have no task_type in CAPABILITY_TASK_TYPES, so the "
        "pickers offer them with submit disabled forever. Map them, or list "
        "them in _INTENTIONALLY_UNMAPPED_CAPABILITIES:\n  "
        + "\n  ".join(unmapped)
    )


# ---------------------------------------------------------------------------
# B7 Step 1: destructured reads of the approvals fan-out
# ---------------------------------------------------------------------------
#
# Inbox, Chat and the AppShell badge all consume `listApprovals()`, but two of
# the three do it destructured (`const { items } = await listApprovals()` in
# AppShell.tsx and chat/page.tsx; `const result = await listApprovals()` +
# `result.items` in inbox/page.tsx). The read-check above
# (`test_api_wrappers_declare_the_fields_pages_read`) cannot see any of these:
# its alias pattern requires a bare identifier (`const (\w+) = await ...`, so
# `const { items }` never binds), its read pattern requires `.value.` (the
# fan-out pages read `result.items`, not `result.value.items`), and
# `listApprovals` returns the NAMED type `ApprovalsResult`, so
# `_declared_return_fields` (which only reads inline `Promise<{...}>`
# literals) never yields its real fields. A rename of
# `ApprovalsResult.items` would therefore break CI nowhere while blanking
# three pages — the same defect class as the audit's `entries`-vs-`messages`
# finding and B1's `r.status`/`r.id`-vs-`state`/`run_id` (a source
# permanently empty while looking implemented).


def _approvals_result_fields() -> set[str]:
    """Field names of the `ApprovalsResult` type literal in approvals.ts."""
    source = (SRC / "lib" / "api" / "approvals.ts").read_text(encoding="utf-8")
    match = re.search(
        r"export\s+type\s+ApprovalsResult\s*=\s*\{(.*?)\n\};", source, re.S
    )
    assert match, "ApprovalsResult is gone from approvals.ts"
    return set(re.findall(r"^\s*([A-Za-z_]\w*)\s*[?]?\s*:", match.group(1), re.M))


_FANOUT_PAGES = (
    APP_DIR / "(app)" / "inbox" / "page.tsx",
    APP_DIR / "(app)" / "chat" / "page.tsx",
    SRC / "components" / "layout" / "AppShell.tsx",
)


def test_fanout_pages_read_declared_approvals_result_fields() -> None:
    """Every `listApprovals()` read on the three fan-out pages is declared.

    Destructured names (`const { items } = await listApprovals()`) and alias
    reads (`result.items`) must both be fields of `ApprovalsResult`. Static
    check (this repo has no frontend unit runner): names are resolved from
    the await call sites, never guessed.
    """
    declared = _approvals_result_fields()
    assert "items" in declared, (
        "ApprovalsResult lost its `items` field; Inbox, Chat and the AppShell "
        "badge all read it."
    )

    offenders: list[str] = []
    for page in _FANOUT_PAGES:
        assert page.exists(), f"fan-out page moved: {page}"
        code = _code_only(page)

        for fields, fn in re.findall(
            r"const\s*\{([^}]*)\}\s*=\s*await\s+(\w+)\s*\(", code
        ):
            if fn != "listApprovals":
                continue
            for name in re.findall(r"[A-Za-z_]\w*", fields):
                if name not in declared:
                    offenders.append(
                        f"{page.relative_to(FRONTEND)}: destructured "
                        f"`{name}` is not an ApprovalsResult field "
                        f"(declares {sorted(declared)})"
                    )

        for alias, fn in re.findall(
            r"const\s+([A-Za-z_]\w*)\s*=\s*await\s+(\w+)\s*\(", code
        ):
            if fn != "listApprovals":
                continue
            for field in re.findall(rf"\b{re.escape(alias)}\.([A-Za-z_]\w*)", code):
                if field not in declared:
                    offenders.append(
                        f"{page.relative_to(FRONTEND)}: {alias}.{field} is not "
                        f"an ApprovalsResult field (declares {sorted(declared)})"
                    )

    assert not offenders, (
        "these fan-out reads are not in the shape listApprovals declares, so "
        "they silently produce undefined:\n  " + "\n  ".join(sorted(set(offenders)))
    )


# ---------------------------------------------------------------------------
# B7 Step 2: envelope strictness for the approval sources
# ---------------------------------------------------------------------------
#
# `test_approval_sources_exist_and_are_readable` proves the documented key
# (`tasks`/`workflows`/`runs`) is PRESENT, but an empty list passes it while
# the page silently takes its `?? []` fallback path. These three tests seed
# one genuinely pending item per source and assert the first pending item
# carries every field the Inbox loader reads (`loadTasks`/`loadWorkflows`/
# `loadAutonomy` in approvals.ts), so a backend rename that drops one blanks
# the Inbox loudly instead of silently. The fourth source, orchestration, is
# NOT repeated here: `test_orchestration_pending_run_carries_the_fields_the_
# inbox_reads` already asserts exactly this (B1 follow-up) — a duplicate
# would only slow the suite.


@pytest.mark.asyncio
async def test_task_envelope_item_carries_the_fields_the_inbox_reads(
    db_a2a_client, db_session_factory
) -> None:
    """A pending task must expose the fields `toTaskPendingItem` reads.

    The Inbox keys the item by `task_id`, filters on `status`, titles it from
    the sender/payload and ages it from `created_at`/`expires_at`. Any of
    those missing and the item either vanishes or renders nameless.
    """
    from app.a2a.models import A2ATask, TaskStatus
    from app.database.repositories import OwnerRepository

    async with db_session_factory() as session:
        owner = await OwnerRepository().get_or_create_default(session)
        session.add(
            A2ATask(
                owner_id=owner.id,
                task_id=f"task_{uuid.uuid4().hex[:8]}",
                sender_agent_id="nexus:ed25519:" + "a" * 32,
                recipient_agent_id="nexus:ed25519:" + "b" * 32,
                status=TaskStatus.PENDING_APPROVAL.value,
                task_type="availability_check",
                purpose="scheduling",
                request_payload={
                    "data_category": "availability",
                    "message": "Can you meet Thursday?",
                },
            )
        )
        await session.commit()

    response = await db_a2a_client.get("/a2a/tasks")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "tasks" in body, (
        f"GET /a2a/tasks lost its documented 'tasks' key; got {sorted(body)}"
    )
    pending = [
        t for t in body["tasks"] if t.get("status") == TaskStatus.PENDING_APPROVAL.value
    ]
    assert pending, (
        "the seeded pending_approval task is missing from /a2a/tasks; the "
        "Inbox would show nothing waiting."
    )
    first = pending[0]
    for field in (
        "task_id",
        "status",
        "sender_agent_id",
        "recipient_agent_id",
        "task_type",
        "purpose",
        "request_payload",
        "created_at",
        "expires_at",
    ):
        assert field in first, (
            f"a pending task must carry {field!r}; toTaskPendingItem reads it. "
            f"Got: {sorted(first)}"
        )


@pytest.mark.asyncio
async def test_workflow_envelope_item_carries_the_fields_the_inbox_reads(
    db_workflow_client, db_session_factory
) -> None:
    """A waiting workflow must expose the fields `loadWorkflows` reads.

    Created over HTTP (proving the create shape), then parked in
    `waiting_approval` directly: reaching that state through the policy ASK
    machinery is a backend-integration concern, while this test is about the
    envelope the Inbox consumes. The Inbox keys by `workflow_id`, filters on
    `status`, and titles/ages from `workflow_type`/`purpose`/`current_step_
    number`/`created_at`/`expires_at`.
    """
    from app.workflows.models import Workflow, WorkflowStatus

    created = await db_workflow_client.post(
        "/workflows",
        json={
            "workflow_type": "meeting_coordination",
            "purpose": "scheduling",
            "steps": [
                {
                    "step_type": "availability_check",
                    "input_payload": {"candidate_slots": ["10:00"]},
                }
            ],
            "ttl_seconds": 3600,
        },
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow_id"]

    async with db_session_factory() as session:
        from sqlalchemy import select

        wf = (
            await session.execute(
                select(Workflow).where(Workflow.workflow_id == workflow_id)
            )
        ).scalar_one()
        wf.status = WorkflowStatus.WAITING_APPROVAL.value
        wf.current_step_number = 1
        await session.commit()

    response = await db_workflow_client.get("/workflows")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "workflows" in body, (
        f"GET /workflows lost its documented 'workflows' key; got {sorted(body)}"
    )
    pending = [
        w
        for w in body["workflows"]
        if w.get("status") == WorkflowStatus.WAITING_APPROVAL.value
    ]
    assert pending, (
        "the parked waiting_approval workflow is missing from /workflows; the "
        "Inbox would show nothing waiting."
    )
    first = pending[0]
    for field in (
        "workflow_id",
        "status",
        "workflow_type",
        "purpose",
        "current_step_number",
        "failure_reason",
        "created_at",
        "expires_at",
    ):
        assert field in first, (
            f"a waiting workflow must carry {field!r}; loadWorkflows reads it. "
            f"Got: {sorted(first)}"
        )


@pytest.mark.asyncio
async def test_autonomy_envelope_item_carries_the_fields_the_inbox_reads(
    db_autonomy_client, db_session_factory
) -> None:
    """A waiting run must expose the fields `loadAutonomy` relies on.

    The Inbox keys the item by `id`, filters on `status`, titles from `goal`
    and summarises from `stop_reason` (the `approval_prompt` fallback the
    page prefers is NOT on `AutonomyRunOut`, so `stop_reason` is what the
    page actually gets — asserted here, not wished for). Deliberately NOT
    asserted: `approval_prompt`/`requested_action`/`risk_level`/`mode`/
    `run_id`, none of which `AutonomyRunOut` returns; `loadAutonomy` reads
    them only through `??` fallbacks to null, so their absence is tolerated
    by design rather than a blanking risk.
    """
    from datetime import datetime, timezone

    from app.autonomy.models import AutonomyRun, RunStatus
    from app.database.repositories import OwnerRepository

    async with db_session_factory() as session:
        owner = await OwnerRepository().get_or_create_default(session)
        session.add(
            AutonomyRun(
                owner_id=owner.id,
                goal="Book the Thursday room",
                status=RunStatus.WAITING_APPROVAL.value,
                stop_reason="Needs owner decision",
                started_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    response = await db_autonomy_client.get("/autonomy/runs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "runs" in body, (
        f"GET /autonomy/runs lost its documented 'runs' key; got {sorted(body)}"
    )
    pending = [
        r for r in body["runs"] if r.get("status") == RunStatus.WAITING_APPROVAL.value
    ]
    assert pending, (
        "the seeded waiting_approval run is missing from /autonomy/runs; the "
        "Inbox would show nothing waiting."
    )
    first = pending[0]
    for field in ("id", "status", "goal", "stop_reason", "created_at"):
        assert field in first, (
            f"a waiting autonomy run must carry {field!r}; loadAutonomy reads "
            f"it. Got: {sorted(first)}"
        )


# ---------------------------------------------------------------------------
# B7 Step 3: deny/approve bodies vs. backend request schemas
# ---------------------------------------------------------------------------
#
# The Inbox (and Chat's inline approvals) decide through ONE function,
# `decideApproval` in approvals.ts, which sends `{ notes: "Approved" }` on
# approve and `{ reason: "Declined by owner" }` on deny to four different
# endpoints — each with its own request schema (`{reason}` vs `{notes}` is
# exactly the kind of per-subsystem vocabulary split the audit found). A body
# the schema rejects is a 422 the page surfaces as "That decision could not
# be recorded", with no hint that the payload was doomed before it was sent.
# These tests read the literal bodies out of `decideApproval` (so a payload
# change is picked up, not silently skipped) and validate each against the
# real Pydantic model the route parses — schemas read from the route files
# first, never guessed (B1's precedent: the orchestration bodies were
# verified against `OrchestrationApproveRequest` and fixed where they
# disagreed).


def _inbox_decision_bodies() -> tuple[dict, dict]:
    """The approve/deny literal bodies `decideApproval` actually sends."""
    source = (SRC / "lib" / "api" / "approvals.ts").read_text(encoding="utf-8")
    anchor = source.find("export async function decideApproval")
    assert anchor != -1, "decideApproval is gone from approvals.ts"
    block = source[anchor : anchor + 1500]
    approve = re.search(r'\?\s*\{\s*notes\s*:\s*"([^"]*)"\s*\}', block)
    deny = re.search(r':\s*\{\s*reason\s*:\s*"([^"]*)"\s*\}', block)
    assert approve and deny, (
        "decideApproval no longer sends {notes}/{reason} literals; re-read "
        "the function and re-pair each body with its route schema."
    )
    return {"notes": approve.group(1)}, {"reason": deny.group(1)}


def test_inbox_decision_bodies_validate_against_backend_schemas() -> None:
    """Each Inbox body must parse as the request model of the route it hits.

    Pairings (route file read first, not guessed): task approve/reject take
    `notes`/`reason` (`TaskApproveRequest`/`TaskRejectRequest`,
    app/schemas/tasks.py); the workflow DENY path is `/cancel`, so its body
    validates against `WorkflowCancelRequest` (app/api/routes/workflows.py);
    orchestration approve/reject are strict (`extra="forbid"`) against
    `OrchestrationApproveRequest`/`OrchestrationRejectRequest`
    (app/orchestration/schemas.py). The path-map assertions pin the pairing:
    if the deny path ever moves off `/cancel`, the schema pairing must move
    with it instead of validating against the wrong model.
    """
    from app.orchestration.schemas import (
        OrchestrationApproveRequest,
        OrchestrationRejectRequest,
    )
    from app.schemas.tasks import TaskApproveRequest, TaskRejectRequest

    from app.api.routes.workflows import WorkflowApproveRequest, WorkflowCancelRequest

    source = (SRC / "lib" / "api" / "approvals.ts").read_text(encoding="utf-8")
    assert "/workflows/${id}/cancel" in source, (
        "the workflow deny path moved off /cancel; re-pair the deny body "
        "with the schema of wherever it points now."
    )
    assert "/orchestration/runs/${id}/approve" in source
    assert "/orchestration/runs/${id}/reject" in source

    approve_body, deny_body = _inbox_decision_bodies()
    assert set(approve_body) == {"notes"}, (
        f"the Inbox approve body gained fields; re-pair it: {approve_body}"
    )
    assert set(deny_body) == {"reason"}, (
        f"the Inbox deny body gained fields; re-pair it: {deny_body}"
    )

    # A body that parses is a body the route accepts; a body that raises is
    # a decision button doomed to 422 (see the autonomy xfail below).
    TaskApproveRequest.model_validate(approve_body)
    TaskRejectRequest.model_validate(deny_body)
    WorkflowApproveRequest.model_validate(approve_body)
    WorkflowCancelRequest.model_validate(deny_body)
    OrchestrationApproveRequest.model_validate(approve_body)
    OrchestrationRejectRequest.model_validate(deny_body)


def _inbox_autonomy_decision_bodies() -> tuple[dict, dict]:
    """The autonomy approve/deny literal bodies `decideApproval` sends.

    Autonomy is the exception in the fan-out: `AutonomyApprovalDecisionRequest`
    requires `approved: bool` and carries `notes` (no `reason`), so the
    autonomy branch sends `{approved, notes}` while the other three sources
    keep `{notes}`/`{reason}` (see `_inbox_decision_bodies`).
    """
    source = (SRC / "lib" / "api" / "approvals.ts").read_text(encoding="utf-8")
    anchor = source.find("export async function decideApproval")
    assert anchor != -1, "decideApproval is gone from approvals.ts"
    block = source[anchor : anchor + 1500]
    approve = re.search(
        r'\{\s*approved\s*:\s*true\s*,\s*notes\s*:\s*"([^"]*)"\s*\}', block
    )
    deny = re.search(
        r'\{\s*approved\s*:\s*false\s*,\s*notes\s*:\s*"([^"]*)"\s*\}', block
    )
    assert approve and deny, (
        "decideApproval no longer sends {approved, notes} autonomy literals; "
        "re-read the function and re-pair each body with its route schema."
    )
    return {"approved": True, "notes": approve.group(1)}, {
        "approved": False,
        "notes": deny.group(1),
    }


def test_inbox_autonomy_decision_bodies_validate_against_backend_schema() -> None:
    """The Inbox autonomy bodies validate against the decision schema.

    B7 locked this as xfail: `decideApproval` sent `{notes}`/`{reason}` but
    `AutonomyApprovalDecisionRequest` requires `approved: bool`, so both
    Inbox autonomy decisions 422'd. Fixed by sending
    `{approved: true, notes}` / `{approved: false, notes}` from the autonomy
    branch of `decideApproval` (and the same flag from the autonomy.ts
    wrappers, which hit the same models). The bodies are read live out of
    `decideApproval`, so a payload change is picked up, not silently skipped.
    """
    from app.autonomy.schemas import AutonomyApprovalDecisionRequest

    approve_body, deny_body = _inbox_autonomy_decision_bodies()
    assert approve_body["approved"] is True
    assert deny_body["approved"] is False
    AutonomyApprovalDecisionRequest.model_validate(approve_body)
    AutonomyApprovalDecisionRequest.model_validate(deny_body)
