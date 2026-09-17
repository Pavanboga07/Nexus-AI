"""M0 regression tests: security and correctness fixes.

Each test in this module corresponds to a concrete defect found in the
repository audit. They are written to FAIL against the pre-fix code:

    C2  handle_inbound_response was defined twice; the surviving copy skipped
        the revoked-sender check, the time-window check and replay recording
    C3  three runtime-fatal call defects (run_workflow, positional
        delegate_task, missing asyncio import)
    C4  a live database credential committed to source (see
        test_no_committed_credentials)
    H1  CORS wildcard with credentials
    H6  WorkflowService.recover_interrupted_workflows() was never called
"""

from __future__ import annotations

import base64
import inspect
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.a2a import signing
from app.a2a.errors import A2AError, A2AErrorCode
from app.a2a.models import A2ATask, TaskStatus, TrustedAgent, TrustStatus
from app.a2a.repository import TaskRepository, TrustedAgentRepository
from app.a2a.schemas import A2AEnvelope, utc_iso_in, utc_now_iso
from app.a2a.service import A2AService
from app.config.settings import Settings
from app.database.models import Base, Owner
from app.identity import crypto
from app.identity.service import IdentityService, PublicIdentity

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class Signer:
    """Real Ed25519 signer implementing the IdentityService surface A2A uses."""

    __test__ = False

    def __init__(self, private_key) -> None:
        self._private_key = private_key

    def get_public_identity(self) -> PublicIdentity:
        raw = crypto.public_key_bytes(self._private_key.public_key())
        return PublicIdentity(
            agent_id=crypto.agent_id_from_public_key(raw),
            public_key=base64.b64encode(raw).decode("ascii"),
            key_algorithm=crypto.KEY_ALGORITHM,
            fingerprint=crypto.fingerprint_from_public_key(raw),
        )

    async def sign(self, data: bytes) -> bytes:
        return crypto.sign_bytes(self._private_key, data)


@pytest.fixture
def responder():
    """A remote agent with a real keypair."""
    priv, pub = crypto.generate_keypair()
    raw = crypto.public_key_bytes(pub)
    return {
        "priv": priv,
        "raw": raw,
        "key_b64": base64.b64encode(raw).decode("ascii"),
        "agent_id": crypto.agent_id_from_public_key(raw),
        "signer": Signer(priv),
    }


@pytest.fixture
def local_agent_id() -> str:
    return "nexus:ed25519:11111111111111111111111111111111"


async def _make_owner(session_factory) -> uuid.UUID:
    async with session_factory() as session:
        owner = Owner(name="m0-regression-owner")
        session.add(owner)
        await session.commit()
        return owner.id


async def _register_trusted(
    session_factory, owner_id, *, agent_id: str, public_key: str, status: str
) -> None:
    async with session_factory() as session:
        await TrustedAgentRepository().add(
            session,
            TrustedAgent(
                owner_id=owner_id,
                agent_id=agent_id,
                public_key=public_key,
                display_name="Responder",
                endpoint="https://example.com/a2a",
                status=status,
            ),
        )
        await session.commit()


async def _make_pending_task(
    session_factory, owner_id, *, local_agent_id: str, remote_agent_id: str
) -> str:
    task_id = f"task_{uuid.uuid4().hex}"
    async with session_factory() as session:
        await TaskRepository().upsert(
            session,
            A2ATask(
                owner_id=owner_id,
                task_id=task_id,
                sender_agent_id=local_agent_id,
                recipient_agent_id=remote_agent_id,
                status=TaskStatus.PENDING.value,
            ),
        )
        await session.commit()
    return task_id


def _build_response_envelope(
    *, task_id: str, sender: str, recipient: str, expires_in: float = 300.0
) -> A2AEnvelope:
    return A2AEnvelope(
        message_id=f"msg_{uuid.uuid4().hex}",
        task_id=task_id,
        sender=sender,
        recipient=recipient,
        timestamp=utc_now_iso(),
        expires_at=utc_iso_in(expires_in),
        message_type="response",
        purpose="scheduling",
        payload={"status": "completed", "available": True},
    )


def _service(session_factory, local_agent_id: str) -> A2AService:
    from unittest.mock import MagicMock

    ident = MagicMock(spec=IdentityService)
    ident.get_public_identity.return_value = PublicIdentity(
        agent_id=local_agent_id,
        public_key="bW9ja19wdWJsaWNfa2V5",
        key_algorithm="Ed25519",
        fingerprint="1111-2222",
    )
    return A2AService(
        session_factory=session_factory,
        identity_service=ident,
        policy_service=MagicMock(),
        memory_manager=None,
        transport=MagicMock(),
        rate_limiter=MagicMock(),
    )


async def _task_status(session_factory, owner_id, task_id: str):
    async with session_factory() as session:
        task = await TaskRepository().get(session, owner_id, task_id)
    return task


# ---------------------------------------------------------------------------
# C2: the response path must enforce the full verification pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_sender_response_is_rejected(
    db_session_factory, local_agent_id, responder
):
    """A REVOKED trusted agent must not be able to complete a task.

    Pre-fix: the surviving duplicate implementation omitted the revoked check
    entirely (it only checked that the record existed), so a revoked peer
    could still drive task completion -> workflow/orchestration callbacks.
    """
    owner_id = await _make_owner(db_session_factory)
    await _register_trusted(
        db_session_factory,
        owner_id,
        agent_id=responder["agent_id"],
        public_key=responder["key_b64"],
        status=TrustStatus.REVOKED.value,
    )
    task_id = await _make_pending_task(
        db_session_factory,
        owner_id,
        local_agent_id=local_agent_id,
        remote_agent_id=responder["agent_id"],
    )

    service = _service(db_session_factory, local_agent_id)
    envelope = _build_response_envelope(
        task_id=task_id, sender=responder["agent_id"], recipient=local_agent_id
    )
    signed = await signing.sign_envelope(responder["signer"], envelope)

    with pytest.raises(A2AError) as exc:
        await service.handle_inbound_response(owner_id, signed)
    assert exc.value.code is A2AErrorCode.REVOKED_SENDER

    task = await _task_status(db_session_factory, owner_id, task_id)
    assert task.status == TaskStatus.PENDING.value
    assert task.response_payload is None


@pytest.mark.asyncio
async def test_expired_response_is_rejected(
    db_session_factory, local_agent_id, responder
):
    """An expired response must be rejected.

    Pre-fix: the surviving implementation never called validate_time_window,
    so stale (replayed) responses were accepted indefinitely.
    """
    owner_id = await _make_owner(db_session_factory)
    await _register_trusted(
        db_session_factory,
        owner_id,
        agent_id=responder["agent_id"],
        public_key=responder["key_b64"],
        status=TrustStatus.ACTIVE.value,
    )
    task_id = await _make_pending_task(
        db_session_factory,
        owner_id,
        local_agent_id=local_agent_id,
        remote_agent_id=responder["agent_id"],
    )

    service = _service(db_session_factory, local_agent_id)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    envelope = A2AEnvelope(
        message_id=f"msg_{uuid.uuid4().hex}",
        task_id=task_id,
        sender=responder["agent_id"],
        recipient=local_agent_id,
        timestamp=past,
        expires_at=past,
        message_type="response",
        purpose="scheduling",
        payload={"status": "completed"},
    )
    signed = await signing.sign_envelope(responder["signer"], envelope)

    with pytest.raises(A2AError) as exc:
        await service.handle_inbound_response(owner_id, signed)
    assert exc.value.code is A2AErrorCode.EXPIRED

    task = await _task_status(db_session_factory, owner_id, task_id)
    assert task.status == TaskStatus.PENDING.value


@pytest.mark.asyncio
async def test_replayed_response_is_ignored_idempotently(
    db_session_factory, local_agent_id, responder
):
    """The same response delivered twice must not be processed twice.

    Pre-fix: the surviving implementation performed no replay recording at
    all, so a replayed response re-fired task-completion callbacks on every
    delivery.

    NOTE (deliberate asymmetry): the request path
    (``handle_inbound``) raises ``A2AErrorCode.REPLAY``, because an HTTP
    caller deserves a 4xx. The response path records the message and returns
    ``None`` on a duplicate - returning a second response to a task we
    already completed is a transport retry, not a caller error. Both paths
    are protected by the same DB-level unique constraint; only the surfaced
    behaviour differs. This test pins the response path's contract.
    """
    from app.a2a.repository import MessageRecordRepository

    owner_id = await _make_owner(db_session_factory)
    await _register_trusted(
        db_session_factory,
        owner_id,
        agent_id=responder["agent_id"],
        public_key=responder["key_b64"],
        status=TrustStatus.ACTIVE.value,
    )
    task_id = await _make_pending_task(
        db_session_factory,
        owner_id,
        local_agent_id=local_agent_id,
        remote_agent_id=responder["agent_id"],
    )

    service = _service(db_session_factory, local_agent_id)
    envelope = _build_response_envelope(
        task_id=task_id, sender=responder["agent_id"], recipient=local_agent_id
    )
    signed = await signing.sign_envelope(responder["signer"], envelope)

    # First delivery is accepted.
    assert await service.handle_inbound_response(owner_id, signed) is None

    # The message_id is durably recorded (this is what blocks the replay).
    async with db_session_factory() as session:
        recorded = await MessageRecordRepository().get_by_message_id(
            session, owner_id, signed.message_id
        )
    assert recorded is not None
    assert recorded.message_id == signed.message_id

    # Second delivery of the identical message_id is discarded, not re-fired.
    assert await service.handle_inbound_response(owner_id, signed) is None

    # Exactly one record exists for this message_id.
    async with db_session_factory() as session:
        again = await MessageRecordRepository().get_by_message_id(
            session, owner_id, signed.message_id
        )
    assert again is not None
    assert again.id == recorded.id


@pytest.mark.asyncio
async def test_response_from_wrong_sender_is_rejected(
    db_session_factory, local_agent_id, responder
):
    """A response must come from the agent the task was sent to."""
    owner_id = await _make_owner(db_session_factory)
    await _register_trusted(
        db_session_factory,
        owner_id,
        agent_id=responder["agent_id"],
        public_key=responder["key_b64"],
        status=TrustStatus.ACTIVE.value,
    )

    # Task addressed to a DIFFERENT agent than the one responding.
    other_priv, other_pub = crypto.generate_keypair()
    other_raw = crypto.public_key_bytes(other_pub)
    other_agent_id = crypto.agent_id_from_public_key(other_raw)

    task_id = await _make_pending_task(
        db_session_factory,
        owner_id,
        local_agent_id=local_agent_id,
        remote_agent_id=other_agent_id,
    )

    service = _service(db_session_factory, local_agent_id)
    envelope = _build_response_envelope(
        task_id=task_id, sender=responder["agent_id"], recipient=local_agent_id
    )
    signed = await signing.sign_envelope(responder["signer"], envelope)

    with pytest.raises(A2AError) as exc:
        await service.handle_inbound_response(owner_id, signed)
    assert exc.value.code is A2AErrorCode.INVALID_RESPONSE

    task = await _task_status(db_session_factory, owner_id, task_id)
    assert task.status == TaskStatus.PENDING.value


@pytest.mark.asyncio
async def test_gateway_delivery_is_tolerant_but_still_verifies(
    db_session_factory, local_agent_id, responder
):
    """The gateway adapter never raises, but still rejects hostile frames."""
    owner_id = await _make_owner(db_session_factory)
    await _register_trusted(
        db_session_factory,
        owner_id,
        agent_id=responder["agent_id"],
        public_key=responder["key_b64"],
        status=TrustStatus.REVOKED.value,
    )
    task_id = await _make_pending_task(
        db_session_factory,
        owner_id,
        local_agent_id=local_agent_id,
        remote_agent_id=responder["agent_id"],
    )
    service = _service(db_session_factory, local_agent_id)
    envelope = _build_response_envelope(
        task_id=task_id, sender=responder["agent_id"], recipient=local_agent_id
    )
    signed = await signing.sign_envelope(responder["signer"], envelope)

    # Revoked sender: dropped silently, never raises into the reader loop.
    assert await service.handle_gateway_delivery(owner_id, signed) is None
    assert await service.handle_gateway_delivery(owner_id, {"not": "an envelope"}) is None


def test_handle_inbound_response_has_a_single_definition():
    """Guards against the duplicate-definition regression (C2).

    Two methods with the same name in one class silently shadow each other;
    the second (weaker) copy previously won.
    """
    source = inspect.getsource(A2AService)
    assert source.count("async def handle_inbound_response(") == 1
    assert "import asyncio" in inspect.getsource(
        __import__("app.a2a.service", fromlist=["x"])
    )


# ---------------------------------------------------------------------------
# C3: runtime-fatal call defects
# ---------------------------------------------------------------------------


def test_autonomy_executor_calls_existing_workflow_methods():
    """run_workflow() never existed; start_workflow() is the entry point (A2)."""
    from app.autonomy import executor as executor_module
    from app.workflows.service import WorkflowService

    src = inspect.getsource(executor_module)
    assert "self._workflow_service.run_workflow(" not in src, (
        "AutonomyExecutor calls a nonexistent method"
    )
    assert "self._workflow_service.start_workflow(" in src

    for name in ("create_workflow", "start_workflow", "advance_workflow"):
        assert hasattr(WorkflowService, name), f"WorkflowService.{name} missing"


def test_autonomy_executor_delegates_with_keyword_arguments():
    """delegate_task is keyword-only after owner_id; positional calls TypeError."""
    from app.autonomy import executor as executor_module
    from app.a2a.service import A2AService

    sig = inspect.signature(A2AService.delegate_task)
    params = list(sig.parameters.values())[1:]  # drop self
    assert params[0].name == "owner_id"
    assert all(
        p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:]
    ), "delegate_task signature changed; update the autonomy executor call site"

    src = inspect.getsource(executor_module)
    assert "req_obj" not in src, "positional TaskDelegateRequest call reintroduced"


# ---------------------------------------------------------------------------
# C4 / H1 / H6
# ---------------------------------------------------------------------------


def test_no_committed_credentials():
    """No live database credential may be committed to source (C4)."""
    offenders: list[str] = []
    forbidden = (
        re.compile(r"postgresql\+asyncpg://[^\s\"']*:[^\s\"'@]+@[^\s\"']*\.neon\.tech"),
        re.compile(r"npg_[A-Za-z0-9]{12,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    )
    for path in REPO_ROOT.rglob("*"):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        if path.suffix not in {".py", ".ts", ".tsx", ".json", ".yml", ".yaml", ".md", ".ini", ".env", ".example"}:
            continue
        if path.name in {".env"}:
            continue  # local, gitignored
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in forbidden:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {pattern.pattern}")
    assert not offenders, "committed secrets detected:\n" + "\n".join(offenders)


def test_cors_never_wildcards_with_credentials():
    """A wildcard origin must never be combined with credentials (H1)."""
    s = Settings()
    assert s.cors_allows_any_origin is False
    assert "*" not in s.cors_origins_list
    assert s.cors_origins_list, "CORS allow-list must not be empty"


def test_workflow_recovery_is_wired_into_startup():
    """recover_interrupted_workflows() must actually be called (H6)."""
    import app.main as main_module

    src = inspect.getsource(main_module)
    assert "recover_interrupted_workflows" in src, (
        "Workflow crash recovery is implemented but never invoked"
    )


def test_gateway_directory_calls_are_ssrf_validated():
    """Directory lookups must pass through validate_endpoint (H8)."""
    from app.api.routes import discovery as discovery_module

    src = inspect.getsource(discovery_module)
    assert "_validate_gateway_url" in src
    assert "quote(" in src, "user-supplied query must be URL-encoded"
