"""M5 regression tests: consistency.

Audit findings covered:

  M10  Route handlers and startup wiring reached into other components'
       private state (``agent._memory``, ``agent._provider``,
       ``a2a_service._trusted``, ``a2a_service._session_factory()``,
       ``agent._sessions``). Private reach-through makes a dependency
       invisible: a rename breaks the caller silently and the coupling never
       shows up in a type check.
  --   Four unrelated error shapes (``A2AError``, ``ToolError``,
       ``PolicyServiceError``, ad-hoc ``HTTPException``) meant the same
       condition produced different statuses and bodies depending on which
       layer caught it, and a missing dependency surfaced as a misleading 500
       rather than a 503.
  --   Authorization was invoked from several places with hand-written
       ``EvaluationRequest`` objects and literal action strings, so a typo in
       an action slug would silently match no policy and degrade to ASK.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.authz import Action, Authorizer, Requester
from app.errors import (
    CODE_DEPENDENCY_UNAVAILABLE,
    CODE_INTERNAL,
    CODE_VALIDATION,
    ConflictError,
    DependencyUnavailableError,
    ErrorKind,
    NexusError,
    NotFoundError,
    ValidationError,
)

APP_DIR = Path(__file__).resolve().parent.parent / "app"


# ---------------------------------------------------------------------------
# Public interfaces instead of private reach-through
# ---------------------------------------------------------------------------


def _code_without_comments(path: Path) -> str:
    """Source with comments removed but LINE STRUCTURE preserved.

    Static assertions must inspect CODE, not prose: historical notes that
    mention the old private call are valuable documentation. Line structure is
    kept so multi-line expressions (``agent.record_exchange(``) still read as
    written.
    """
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            lines.append("")
            continue
        # Drop trailing comments crudely but safely: only when the '#' is not
        # inside quotes, which is adequate for these assertions.
        if "#" in line and line.count('"') % 2 == 0 and line.count("'") % 2 == 0:
            line = line.split("#", 1)[0]
        lines.append(line)
    return "\n".join(lines)


#: Modules that legitimately use their own private members.
_SELF_OWNING_MODULES = {
    "agent\\agent.py",
    "a2a\\service.py",
    "tools\\service.py",
    "policy\\service.py",
    "identity\\service.py",
}


def test_no_cross_module_private_reach_through() -> None:
    """Nothing outside a module may use another component's private members.

    A module using its own private attributes is fine (that is what private
    means); the bug is *another* module reaching in. So the scan skips the
    file that defines the component.
    """
    offenders: list[str] = []
    per_component = {
        "agent": r"\bagent\._[a-z_]+",
        "a2a_service": r"\ba2a_service\._[a-z_]+",
        "orchestrator": r"\borchestrator\._[a-z_]+",
        "autonomy_service": r"\bautonomy_service\._[a-z_]+",
        "workflow_service": r"\bworkflow_service\._[a-z_]+",
        "memory_manager": r"\bmemory_manager\._[a-z_]+",
        "tool_service": r"\btool_service\._[a-z_]+",
        "identity_service": r"\bidentity_service\._[a-z_]+",
        "policy_service": r"\bpolicy_service\._[a-z_]+",
    }
    for path in APP_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(APP_DIR.parent))
        # Skip the module that owns the attribute.
        if any(rel.endswith(owner) for owner in _SELF_OWNING_MODULES):
            continue
        code = _code_without_comments(path)
        for lineno, line in enumerate(code.splitlines(), start=1):
            for component, pattern in per_component.items():
                for match in re.finditer(pattern, line):
                    offenders.append(f"{rel}:{lineno}: {match.group(0)}")
    assert not offenders, "private reach-through remains:\n" + "\n".join(offenders)


def test_agent_exposes_its_collaborators_publicly() -> None:
    """The collaborators callers need are public, and still work."""
    from app.agent.agent import NexusAgent

    for name in ("memory", "provider", "sessions", "memory_enabled", "provider_name"):
        assert hasattr(NexusAgent, name), f"NexusAgent.{name} is missing"


def test_a2a_service_exposes_registry_publicly() -> None:
    from app.a2a.service import A2AService

    assert hasattr(A2AService, "trusted_agents")
    assert hasattr(A2AService, "session_factory")
    assert hasattr(A2AService, "list_trusted_agent_ids")


def test_agent_owner_id_is_public() -> None:
    from app.agent.agent import NexusAgent

    assert hasattr(NexusAgent, "owner_id")
    assert not hasattr(NexusAgent, "_owner_id")


def test_agent_has_no_private_session_reach_through_in_routes() -> None:
    """Routes must record exchanges through the agent, not its store."""
    chat = _code_without_comments(APP_DIR / "api" / "routes" / "chat.py")
    assert "agent._sessions" not in chat
    assert "agent._schedule_extraction" not in chat
    assert "agent.record_exchange" in chat


def test_discovery_route_uses_public_trust_lookup() -> None:
    src = _code_without_comments(APP_DIR / "api" / "routes" / "discovery.py")
    assert "a2a_service._trusted" not in src
    assert "a2a_service._session_factory" not in src
    assert "list_trusted_agent_ids" in src


# ---------------------------------------------------------------------------
# One error envelope
# ---------------------------------------------------------------------------


def test_every_domain_error_shares_one_envelope_shape() -> None:
    """Same keys, same nesting, for every expected failure."""
    errors = [
        NexusError("boom"),
        ValidationError("bad input"),
        NotFoundError("gone"),
        ConflictError("already there"),
        DependencyUnavailableError("no database"),
    ]
    for exc in errors:
        body = exc.to_dict()
        assert set(body) == {"error"}, body
        assert set(body["error"]) >= {"code", "message", "kind"}, body
        assert isinstance(body["error"]["code"], str) and body["error"]["code"]
        assert isinstance(body["error"]["message"], str)
        assert isinstance(exc.http_status, int)


def test_error_status_matches_the_kind() -> None:
    assert ValidationError("x").http_status == 400
    assert NotFoundError("x").http_status == 404
    assert ConflictError("x").http_status == 409
    # The important one: a missing dependency is 503, NOT 500.
    assert DependencyUnavailableError("x").http_status == 503
    assert DependencyUnavailableError("x").code == CODE_DEPENDENCY_UNAVAILABLE


def test_nexus_error_never_leaks_internals_by_default() -> None:
    """A generic internal error must not echo the exception text."""
    generic = NexusError("An unexpected error occurred.")
    assert generic.code == CODE_INTERNAL
    assert generic.http_status == 500
    # The message is written to be safe; details are opt-in and explicit.
    assert generic.details == {}


@pytest.mark.asyncio
async def test_validation_errors_use_the_shared_envelope(client) -> None:
    """A 422 must parse identically to every other error."""
    resp = await client.post("/chat", json={"session_id": "x", "message": "   "})
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == CODE_VALIDATION
    assert body["error"]["kind"] == ErrorKind.VALIDATION.value
    # The useful detail (which field failed) is preserved, not discarded.
    assert "errors" in body["error"]["details"]


@pytest.mark.asyncio
async def test_missing_dependency_is_503_not_500(db_client) -> None:
    """A missing subsystem must not masquerade as an internal error.

    ``get_identity_service`` used to raise a bare RuntimeError, which the
    catch-all handler reported as 500 with an opaque message.
    """
    resp = await db_client.get("/identity")
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"]["code"] == CODE_DEPENDENCY_UNAVAILABLE


@pytest.mark.asyncio
async def test_unknown_route_still_404(client) -> None:
    """Framework-level errors are unaffected by the domain handler."""
    assert (await client.get("/definitely-not-a-route")).status_code == 404


# ---------------------------------------------------------------------------
# One authorization entry point
# ---------------------------------------------------------------------------


def test_action_vocabulary_is_closed_and_matches_stored_rules() -> None:
    """The slugs the engine matches on live in exactly one place."""
    from app.policy.models import KNOWN_ACTIONS

    assert Action.ACCESS_TOOL in Action.ALL
    assert Action.DISCLOSE_INFORMATION in Action.ALL
    # Every action the policy vocabulary documents must be expressible.
    for action in ("read_memory", "disclose_information", "send_message",
                   "create_event", "modify_event", "access_tool", "custom"):
        assert action in Action.ALL, f"{action} missing from Action.ALL"
    # And the two vocabularies must not silently diverge.
    missing = KNOWN_ACTIONS - Action.ALL
    assert not missing, f"policy knows actions the Authorizer does not: {missing}"


@pytest.mark.asyncio
async def test_authorizer_rejects_an_unknown_action(db_session_factory, db_owner_id) -> None:
    """A typo'd action fails loudly instead of silently degrading to ASK.

    This is the whole reason the vocabulary is centralised: an unknown slug
    matches no stored rule, so a silent fallback would look like a legitimate
    "needs approval" rather than a bug.
    """
    from app.policy.service import PolicyService

    authorizer = Authorizer(PolicyService(session_factory=db_session_factory))
    with pytest.raises(ValueError):
        await authorizer.check(
            db_owner_id,
            Requester.local(),
            "not_a_real_action",
            data_category="chat",
            purpose="assistant",
        )


@pytest.mark.asyncio
async def test_authorizer_returns_the_engines_three_way_decision(
    db_session_factory, db_owner_id
) -> None:
    """ALLOW / ASK / DENY all survive the wrapper unchanged."""
    from app.policy.service import PolicyService

    authorizer = Authorizer(PolicyService(session_factory=db_session_factory))

    # No rule at all -> ASK (secure default), not an exception.
    result = await authorizer.check(
        db_owner_id,
        Requester.remote("nexus:ed25519:" + "a" * 32),
        Action.DISCLOSE_INFORMATION,
        data_category="availability",
        purpose="scheduling",
    )
    assert authorizer.requires_approval(result) is True
    assert authorizer.allowed(result) is False
    assert authorizer.denied(result) is False


def test_requester_self_is_a_distinct_policy_subject() -> None:
    """A locally-initiated action is not the same subject as a remote agent."""
    assert Requester.local().agent_id == Requester.SELF
    assert Requester.remote("nexus:ed25519:" + "b" * 32).agent_id != Requester.SELF


def test_tool_service_uses_the_authorizer() -> None:
    from app.tools import service as tool_service_module

    src = inspect.getsource(tool_service_module)
    assert "self._authorizer.check(" in src
    assert "EvaluationRequest(" not in src, (
        "ToolService builds its own policy request; it should use the Authorizer"
    )
