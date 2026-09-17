"""M3 regression tests: the authenticated edge.

Audit finding C1: the API had NO authentication whatsoever. Two consequences
that these tests pin shut:

  1. Every endpoint was reachable by anyone who could reach the port, with full
     owner authority - memory, identity, tools, autonomy, LLM spend.
  2. Authorization was self-service: a caller could simply POST a wildcard
     ALLOW policy and then do whatever the policy engine was supposed to gate.

Also covered: the owner was resolved once at startup from the FIRST row in
``owners`` and cached for the process lifetime, so every request served the
same principal. That is the root cause of the missing multi-tenancy, and it is
why ``test_tenant_isolation_*`` below is the most important test in this file.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.auth import crypto
from app.auth.service import AuthService

PASSWORD = "correct-horse-battery-staple"


async def _register(client, email: str, password: str = PASSWORD) -> dict:
    resp = await client.post(
        "/auth/register", json={"email": email, "password": password}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# The edge is closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_endpoints_reject_unauthenticated_callers(authed_client):
    """Without a session, data endpoints must return 401 (not data)."""
    protected = [
        ("GET", "/sessions"),
        ("POST", "/sessions"),
        ("GET", "/memories"),
        ("GET", "/tools"),
        ("GET", "/policy"),
        ("GET", "/consent"),
        ("GET", "/a2a/agents"),
        ("GET", "/a2a/audit/list"),
        ("GET", "/a2a/tasks"),
        ("GET", "/workflows"),
        ("GET", "/autonomy/config"),
        ("GET", "/orchestration/runs"),
        ("GET", "/system/status"),
    ]
    for method, path in protected:
        resp = await authed_client.request(method, path)
        assert resp.status_code == 401, (
            f"{method} {path} returned {resp.status_code}, expected 401"
        )


@pytest.mark.asyncio
async def test_self_service_wildcard_policy_requires_a_session(authed_client):
    """The escalation path itself: you cannot mint authorization without auth.

    Pre-fix: anyone could POST a wildcard ALLOW policy, which made the
    (otherwise sound) policy engine meaningless.
    """
    resp = await authed_client.post(
        "/policy",
        json={
            "requester_agent_id": "*",
            "data_category": "*",
            "action": "*",
            "purpose": "*",
            "decision": "ALLOW",
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_public_endpoints_stay_public(authed_client):
    """/health and the identity card must remain reachable without a session.

    Health is the liveness contract for load balancers and orchestrators; the
    card is how a peer discovers our public key. Neither may require a session,
    and neither may leak configuration to an anonymous caller - which is why
    /health returns only {status, version} and the detailed subsystem report
    moved to the protected /system/status.
    """
    health = await authed_client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert set(body) == {"status", "version"}, (
        f"/health leaks detail to anonymous callers: {sorted(body)}"
    )

    # The public card endpoint is reachable without a session (it returns 503
    # here only because this fixture configures no identity - importantly NOT
    # 401, which would make the agent undiscoverable).
    card = await authed_client.get("/.well-known/nexus-agent.json")
    assert card.status_code != 401, card.text


@pytest.mark.asyncio
async def test_auth_status_is_reachable_and_reports_enforcement(authed_client):
    resp = await authed_client.get("/auth/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_required"] is True
    assert body["authenticated"] is False


# ---------------------------------------------------------------------------
# Registration / login / logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_then_access_data(authed_client):
    body = await _register(authed_client, "alice@example.com")
    assert body["user"]["email"] == "alice@example.com"
    assert body["token"]

    # The cookie was set by the response, so the same client is now signed in.
    sessions = await authed_client.get("/sessions")
    assert sessions.status_code == 200

    me = await authed_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_register_sets_httponly_session_cookie(authed_client):
    resp = await authed_client.post(
        "/auth/register",
        json={"email": "cookie@example.com", "password": PASSWORD},
    )
    assert resp.status_code == 201
    set_cookie = resp.headers.get("set-cookie", "")
    assert "nexus_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie or "samesite=lax" in set_cookie.lower()


@pytest.mark.asyncio
async def test_duplicate_registration_is_rejected(authed_client):
    await _register(authed_client, "dup@example.com")
    resp = await authed_client.post(
        "/auth/register",
        json={"email": "DUP@example.com", "password": PASSWORD},
    )
    # Case-insensitive: the same address cannot register twice.
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_weak_password_is_rejected(authed_client):
    resp = await authed_client.post(
        "/auth/register", json={"email": "weak@example.com", "password": "short"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_login_with_wrong_password_is_rejected(authed_client):
    await _register(authed_client, "bob@example.com")
    await authed_client.post("/auth/logout")

    resp = await authed_client.post(
        "/auth/login", json={"email": "bob@example.com", "password": "wrong-password"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_email_is_indistinguishable_from_wrong_password(
    authed_client,
):
    """No user enumeration: same status and same message either way."""
    await _register(authed_client, "known@example.com")
    await authed_client.post("/auth/logout")

    unknown = await authed_client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    wrong = await authed_client.post(
        "/auth/login", json={"email": "known@example.com", "password": "nope-nope-nope"}
    )
    assert unknown.status_code == wrong.status_code == 401
    # Compared through the envelope the client reads. Equality of the whole
    # error object is the point: a differing `code`, `message`, or `details`
    # would let an attacker tell "no such account" from "wrong password".
    assert unknown.json()["error"] == wrong.json()["error"]


@pytest.mark.asyncio
async def test_login_succeeds_with_correct_password(authed_client):
    await _register(authed_client, "carol@example.com")
    await authed_client.post("/auth/logout")
    assert (await authed_client.get("/sessions")).status_code == 401

    resp = await authed_client.post(
        "/auth/login", json={"email": "carol@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 200
    assert (await authed_client.get("/sessions")).status_code == 200


@pytest.mark.asyncio
async def test_logout_revokes_the_session_immediately(authed_client):
    """Logout must invalidate server-side, not just clear the cookie."""
    await _register(authed_client, "dave@example.com")
    assert (await authed_client.get("/sessions")).status_code == 200

    out = await authed_client.post("/auth/logout")
    assert out.status_code == 200
    assert out.json()["logged_out"] is True
    assert (await authed_client.get("/sessions")).status_code == 401


@pytest.mark.asyncio
async def test_tampered_cookie_is_rejected(authed_client):
    await _register(authed_client, "eve@example.com")
    good = authed_client.cookies.get("nexus_session")
    assert good

    # Flip a byte in the opaque token half.
    token_part, signed_part = good.split(".", 1)
    tampered = ("A" if token_part[0] != "A" else "B") + token_part[1:]
    authed_client.cookies.set("nexus_session", f"{tampered}.{signed_part}")

    assert (await authed_client.get("/sessions")).status_code == 401


@pytest.mark.asyncio
async def test_forged_signature_is_rejected(authed_client):
    """A payload signed with the wrong key must not authenticate."""
    forged = crypto.sign_session_payload(
        {
            "sub": str(uuid.uuid4()),
            "iat": 0,
            "exp": 4102444800,
            "jti": "forged",
        },
        secret="attacker-controlled-secret",
    )
    authed_client.cookies.set("nexus_session", f"whatever.{forged}")
    assert (await authed_client.get("/sessions")).status_code == 401


@pytest.mark.asyncio
async def test_expired_session_is_rejected(db_session_factory, authed_app):
    """An expired token must not authenticate."""
    import httpx

    service = AuthService(
        session_factory=db_session_factory,
        session_secret=authed_app.state.auth_service._secret,
        session_ttl_seconds=-1,  # already expired
        allow_registration=True,
    )
    issued = await service.register(
        email="expired@example.com", password=PASSWORD
    )
    transport = httpx.ASGITransport(app=authed_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"nexus_session": issued.token},
    ) as client:
        assert (await client.get("/sessions")).status_code == 401


@pytest.mark.asyncio
async def test_bearer_token_works_for_non_browser_clients(authed_client):
    """Non-browser clients (SDKs, scripts) authenticate with a bearer token."""
    body = await _register(authed_client, "api@example.com")
    token = body["token"]
    # Drop the cookie WITHOUT logging out, so the session is still live and the
    # only credential left is the bearer header.
    authed_client.cookies.clear()

    resp = await authed_client.get(
        "/sessions", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TENANT ISOLATION - the actual multi-tenancy test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_isolation_conversations(authed_client):
    """User B must not be able to read user A's conversation.

    Pre-fix this was untestable: there was exactly one principal, so there was
    nothing to isolate. Now each registered user is a distinct owner, and the
    repository's owner scoping is exercised through a real authenticated HTTP
    path.
    """
    # Alice registers, creates a session, then signs out.
    await _register(authed_client, "alice-tenant@example.com")
    alice_session = (
        await authed_client.post("/sessions")
    ).json()["session_id"]
    assert (await authed_client.get(f"/sessions/{alice_session}")).status_code == 200
    await authed_client.post("/auth/logout")

    # Bob registers and must not see or read Alice's session.
    await _register(authed_client, "bob-tenant@example.com")
    assert (await authed_client.get(f"/sessions/{alice_session}")).status_code == 404
    listed = (await authed_client.get("/sessions")).json()
    assert alice_session not in listed

    # Nor may he delete it.
    assert (
        await authed_client.delete(f"/sessions/{alice_session}")
    ).status_code == 404

    # Alice signs back in and still owns it.
    await authed_client.post("/auth/logout")
    await authed_client.post(
        "/auth/login", json={"email": "alice-tenant@example.com", "password": PASSWORD}
    )
    assert (await authed_client.get(f"/sessions/{alice_session}")).status_code == 200


@pytest.mark.asyncio
async def test_tenant_isolation_policies(authed_client):
    """A policy created by A must be invisible to B."""
    await _register(authed_client, "alice-policy@example.com")
    created = await authed_client.post(
        "/policy",
        json={
            "requester_agent_id": "nexus:ed25519:" + "a" * 32,
            "data_category": "availability",
            "action": "read_memory",
            "purpose": "scheduling",
            "decision": "ALLOW",
        },
    )
    assert created.status_code in (200, 201), created.text
    policy_id = created.json()["id"]
    alice_count = len((await authed_client.get("/policy")).json()["policies"])
    await authed_client.post("/auth/logout")

    await _register(authed_client, "bob-policy@example.com")
    bob_count = len((await authed_client.get("/policy")).json()["policies"])
    assert bob_count == 0
    assert alice_count == 1
    # And B cannot read or delete A's policy by id.
    assert (await authed_client.delete(f"/policy/{policy_id}")).status_code == 404


@pytest.mark.asyncio
async def test_each_registered_user_gets_a_distinct_owner(authed_client):
    a = await _register(authed_client, "distinct-a@example.com")
    await authed_client.post("/auth/logout")
    b = await _register(authed_client, "distinct-b@example.com")
    assert a["user"]["owner_id"] != b["user"]["owner_id"]


# ---------------------------------------------------------------------------
# Password change revokes sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_change_revokes_all_sessions(authed_client):
    await _register(authed_client, "rotate@example.com")
    old_token = authed_client.cookies.get("nexus_session")

    resp = await authed_client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": "a-brand-new-passphrase"},
    )
    assert resp.status_code == 200

    # Old session is dead.
    other = authed_client
    other.cookies.set("nexus_session", old_token)
    assert (await other.get("/sessions")).status_code == 401

    # New password works.
    resp = await authed_client.post(
        "/auth/login",
        json={"email": "rotate@example.com", "password": "a-brand-new-passphrase"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_password_change_requires_current_password(authed_client):
    await _register(authed_client, "rotate2@example.com")
    resp = await authed_client.post(
        "/auth/password",
        json={"current_password": "not-the-password", "new_password": "another-new-passphrase"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Structural guards
# ---------------------------------------------------------------------------


def test_owner_is_not_resolved_at_startup_any_more():
    """The startup owner-cache is what made the API single-tenant."""
    import app.main as main_module

    src = inspect.getsource(main_module.lifespan)
    assert "get_or_create_default(session)\n" not in src.split("Authentication (M3)")[-1], (
        "lifespan must not resolve-and-cache the owner after the auth block"
    )


def test_every_data_router_is_mounted_with_auth_dependency():
    """A new data router must not accidentally be public."""
    import app.main as main_module

    src = inspect.getsource(main_module.create_app)
    for router_var in (
        "chat_router",
        "memories_router",
        "identity_router",
        "policy_router",
        "tools_router",
        "a2a_router",
        "tasks_router",
        "workflows_router",
        "autonomy_router",
        "orchestration_router",
    ):
        assert f"include_router({router_var}, dependencies=protected)" in src, (
            f"{router_var} is mounted without the auth dependency"
        )


def test_session_tokens_are_not_stored_in_plaintext():
    """Only a fingerprint may be persisted, so a DB leak yields no sessions."""
    from app.auth.models import AuthSession

    columns = {c.name for c in AuthSession.__table__.columns}
    assert "token_fingerprint" in columns
    assert "token" not in columns
    src = inspect.getsource(crypto.token_fingerprint)
    assert "sha256" in src
