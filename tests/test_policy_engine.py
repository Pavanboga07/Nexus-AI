"""Policy engine tests: pure decision logic (no DB) + service integration.

The pure tests build in-memory Policy/Consent rows and assert deterministic
decisions. Service tests exercise persistence, consent consumption, the
consumption race, and owner isolation against PostgreSQL.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.database.models import Owner
from app.policy.engine import (
    EvaluationRequest,
    PolicyEngine,
)
from app.policy.models import Policy, PolicyDecision
from app.policy.service import PolicyService, PolicyServiceError

RAHUL = "nexus:ed25519:rahul00000000000000000000000000000000"
PRIYA = "nexus:ed25519:priya00000000000000000000000000000000"

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def _policy(
    *,
    requester=RAHUL,
    data="availability",
    action="disclose_information",
    purpose="scheduling",
    decision="ALLOW",
    scope="summary",
    priority=0,
    starts_at=None,
    expires_at=None,
) -> Policy:
    return Policy(
        owner_id=uuid.uuid4(),
        requester_agent_id=requester,
        data_category=data,
        action=action,
        purpose=purpose,
        decision=decision,
        disclosure_scope=scope,
        priority=priority,
        starts_at=starts_at,
        expires_at=expires_at,
    )


def _consent(
    *,
    requester=RAHUL,
    data="availability",
    action="disclose_information",
    purpose="scheduling",
    decision="ALLOW",
    scope="summary",
    expires_at=None,
    single_use=False,
    used_at=None,
    created_at=None,
) -> "ConsentT":
    from app.policy.models import Consent

    return Consent(
        owner_id=uuid.uuid4(),
        requester_agent_id=requester,
        data_category=data,
        action=action,
        purpose=purpose,
        decision=decision,
        disclosure_scope=scope,
        expires_at=expires_at,
        single_use=single_use,
        used_at=used_at,
        created_at=created_at or NOW - timedelta(minutes=1),
    )


type ConsentT = object  # placeholder for typing; real class imported above


def _request(
    *,
    requester=RAHUL,
    data="availability",
    action="disclose_information",
    purpose="scheduling",
) -> EvaluationRequest:
    return EvaluationRequest(
        requester_agent_id=requester,
        data_category=data,
        action=action,
        purpose=purpose,
    )


ENGINE = PolicyEngine()


# --- Basics ------------------------------------------------------------------


def test_allow_policy_matches() -> None:
    rule = _policy()
    result = ENGINE.evaluate(_request(), policies=[rule], consents=[], now=NOW)
    assert result.decision is PolicyDecision.ALLOW
    assert result.disclosure_scope is not None
    assert result.disclosure_scope.value == "summary"
    assert result.matched_policy_id == str(rule.id)


def test_deny_policy_matches() -> None:
    rule = _policy(decision="DENY", scope="none")
    result = ENGINE.evaluate(_request(), policies=[rule], consents=[], now=NOW)
    assert result.decision is PolicyDecision.DENY


def test_ask_when_no_policy_exists() -> None:
    result = ENGINE.evaluate(_request(), policies=[], consents=[], now=NOW)
    assert result.decision is PolicyDecision.ASK
    assert result.requires_user_approval is True
    assert result.disclosure_scope is None


def test_expired_policy_ignored() -> None:
    rule = _policy(expires_at=NOW - timedelta(hours=1))
    result = ENGINE.evaluate(_request(), policies=[rule], consents=[], now=NOW)
    assert result.decision is PolicyDecision.ASK


def test_future_policy_ignored() -> None:
    rule = _policy(starts_at=NOW + timedelta(hours=1))
    result = ENGINE.evaluate(_request(), policies=[rule], consents=[], now=NOW)
    assert result.decision is PolicyDecision.ASK


def test_purpose_mismatch_falls_through() -> None:
    rule = _policy(purpose="scheduling")
    result = ENGINE.evaluate(
        _request(purpose="marketing"), policies=[rule], consents=[], now=NOW
    )
    # The scheduling rule must NOT answer a marketing request.
    assert result.decision is PolicyDecision.ASK
    assert result.matched_policy_id is None


def test_action_mismatch_falls_through() -> None:
    rule = _policy(action="disclose_information")
    result = ENGINE.evaluate(
        _request(action="send_message"), policies=[rule], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.ASK


def test_category_mismatch_falls_through() -> None:
    rule = _policy(data="availability")
    result = ENGINE.evaluate(
        _request(data="location"), policies=[rule], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.ASK


# --- Sensitive defaults -----------------------------------------------------------


def test_financial_is_deny_by_default() -> None:
    result = ENGINE.evaluate(
        _request(data="financial"), policies=[], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.DENY


def test_private_is_deny_by_default() -> None:
    result = ENGINE.evaluate(
        _request(data="private"), policies=[], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.DENY


def test_wildcard_category_cannot_unlock_financial() -> None:
    """A broad ALLOW-everything rule must not leak financial/private data."""
    broad = _policy(requester="*", data="*", action="*", purpose="*", decision="ALLOW", scope="exact")
    result = ENGINE.evaluate(
        _request(data="financial"), policies=[broad], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.DENY


def test_exact_financial_policy_still_works() -> None:
    rule = _policy(data="financial", decision="ALLOW", scope="summary")
    result = ENGINE.evaluate(
        _request(data="financial"), policies=[rule], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.ALLOW


# --- Precedence --------------------------------------------------------------------


def test_exact_requester_beats_wildcard() -> None:
    wildcard_allow = _policy(requester="*", decision="ALLOW")
    exact_deny = _policy(requester=RAHUL, decision="DENY")
    result = ENGINE.evaluate(
        _request(), policies=[wildcard_allow, exact_deny], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.DENY


def test_exact_purpose_beats_generic_purpose() -> None:
    generic = _policy(purpose="*", decision="ALLOW")
    exact_ask = _policy(purpose="scheduling", decision="ASK")
    result = ENGINE.evaluate(
        _request(), policies=[generic, exact_ask], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.ASK


def test_higher_priority_wins_within_level() -> None:
    low_allow = _policy(decision="ALLOW", priority=1)
    high_deny = _policy(decision="DENY", priority=10)
    result = ENGINE.evaluate(
        _request(), policies=[low_allow, high_deny], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.DENY


def test_higher_priority_allow_beats_lower_priority_deny() -> None:
    """Explicit higher-priority ALLOW resolves an equal-specificity DENY."""
    deny = _policy(decision="DENY", priority=1)
    allow = _policy(decision="ALLOW", priority=10)
    result = ENGINE.evaluate(
        _request(), policies=[deny, allow], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.ALLOW


def test_deny_wins_equal_priority_conflict() -> None:
    allow = _policy(decision="ALLOW")
    deny = _policy(decision="DENY")
    result = ENGINE.evaluate(
        _request(), policies=[allow, deny], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.DENY


def test_expired_high_priority_rule_ignored() -> None:
    expired_deny = _policy(decision="DENY", priority=100, expires_at=NOW - timedelta(minutes=1))
    allow = _policy(decision="ALLOW")
    result = ENGINE.evaluate(
        _request(), policies=[expired_deny, allow], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.ALLOW


def test_full_specificity_ladder() -> None:
    """Every documented ladder level in one evaluation: the most specific
    (requester+data+action+purpose) wins."""
    l8 = _policy(requester="*", data="*", action="*", purpose="*", decision="ALLOW")
    l7 = _policy(requester="*", data="availability", decision="DENY")
    l5 = _policy(requester="*", purpose="scheduling", decision="ASK")
    l4 = _policy(
        requester=RAHUL, data="*", action="*", purpose="*", decision="ALLOW", priority=99
    )
    l1 = _policy(requester=RAHUL, purpose="scheduling", decision="DENY")
    result = ENGINE.evaluate(
        _request(), policies=[l8, l7, l5, l4, l1], consents=[], now=NOW
    )
    assert result.decision is PolicyDecision.DENY
    assert result.matched_policy_id == str(l1.id)


# --- Consents (pure engine) ------------------------------------------------------------


def test_consent_allow_beats_policy() -> None:
    policy = _policy(decision="DENY")
    consent = _consent(decision="ALLOW")
    result = ENGINE.evaluate(
        _request(), policies=[policy], consents=[consent], now=NOW
    )
    assert result.decision is PolicyDecision.ALLOW
    assert result.matched_consent_id == str(consent.id)


def test_consent_deny_wins() -> None:
    policy = _policy(decision="ALLOW")
    consent = _consent(decision="DENY")
    result = ENGINE.evaluate(
        _request(), policies=[policy], consents=[consent], now=NOW
    )
    assert result.decision is PolicyDecision.DENY


def test_consumed_consent_ignored() -> None:
    consent = _consent(single_use=True, used_at=NOW - timedelta(minutes=5))
    result = ENGINE.evaluate(
        _request(), policies=[], consents=[consent], now=NOW
    )
    assert result.decision is PolicyDecision.ASK


def test_expired_consent_ignored() -> None:
    consent = _consent(expires_at=NOW - timedelta(minutes=5))
    result = ENGINE.evaluate(
        _request(), policies=[], consents=[consent], now=NOW
    )
    assert result.decision is PolicyDecision.ASK


def test_consent_requires_exact_match() -> None:
    """Consents are concrete: a Priya consent must not serve a Rahul request."""
    consent = _consent(requester=PRIYA, decision="ALLOW")
    result = ENGINE.evaluate(
        _request(), policies=[], consents=[consent], now=NOW
    )
    assert result.decision is PolicyDecision.ASK


def test_consent_purpose_must_match() -> None:
    consent = _consent(purpose="scheduling")
    result = ENGINE.evaluate(
        _request(purpose="marketing"), policies=[], consents=[consent], now=NOW
    )
    assert result.decision is PolicyDecision.ASK


def test_newest_consent_wins() -> None:
    older_allow = _consent(decision="ALLOW", created_at=NOW - timedelta(hours=2))
    newer_deny = _consent(decision="DENY", created_at=NOW - timedelta(minutes=1))
    result = ENGINE.evaluate(
        _request(), policies=[], consents=[older_allow, newer_deny], now=NOW
    )
    assert result.decision is PolicyDecision.DENY


def test_determinism_same_inputs_same_decision() -> None:
    rules = [
        _policy(decision="DENY"),
        _policy(requester="*", decision="ALLOW", priority=5),
    ]
    first = ENGINE.evaluate(_request(), policies=rules, consents=[], now=NOW)
    second = ENGINE.evaluate(_request(), policies=rules, consents=[], now=NOW)
    assert first == second


# --- Service integration (PostgreSQL) --------------------------------------------------
# (policy_service fixture now lives in conftest.py)


async def test_service_purpose_limitation_end_to_end(policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    await policy_service.create_policy(
        owner_a,
        requester_agent_id=RAHUL,
        data_category="availability",
        action="disclose_information",
        purpose="scheduling",
        decision="ALLOW",
        disclosure_scope="summary",
    )
    allow_result = await policy_service.evaluate(
        owner_a,
        EvaluationRequest(
            requester_agent_id=RAHUL,
            data_category="availability",
            action="disclose_information",
            purpose="scheduling",
        ),
    )
    assert allow_result.decision is PolicyDecision.ALLOW

    marketing = await policy_service.evaluate(
        owner_a,
        EvaluationRequest(
            requester_agent_id=RAHUL,
            data_category="availability",
            action="disclose_information",
            purpose="marketing",
        ),
    )
    assert marketing.decision is PolicyDecision.ASK
    assert marketing.matched_policy_id is None


async def test_one_time_consumption_and_reuse(policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    consent = await policy_service.create_consent(
        owner_a,
        requester_agent_id=RAHUL,
        data_category="location",
        action="disclose_information",
        purpose="travel",
        decision="ALLOW",
        single_use=True,
    )

    first = await policy_service.evaluate(
        owner_a,
        EvaluationRequest(
            requester_agent_id=RAHUL,
            data_category="location",
            action="disclose_information",
            purpose="travel",
        ),
    )
    assert first.decision is PolicyDecision.ALLOW
    assert first.matched_consent_id == str(consent.id)

    # Location is not sensitive-default-deny, so reuse falls back to ASK.
    second = await policy_service.evaluate(
        owner_a,
        EvaluationRequest(
            requester_agent_id=RAHUL,
            data_category="location",
            action="disclose_information",
            purpose="travel",
        ),
    )
    assert second.decision is PolicyDecision.ASK
    assert second.matched_consent_id is None


async def test_concurrent_single_use_consent_safe(policy_service, owner_ids) -> None:
    """Two simultaneous evaluations; exactly one may consume the consent."""
    owner_a, _ = owner_ids
    await policy_service.create_consent(
        owner_a,
        requester_agent_id=RAHUL,
        data_category="availability",
        action="disclose_information",
        purpose="scheduling",
        decision="ALLOW",
        single_use=True,
    )
    request = EvaluationRequest(
        requester_agent_id=RAHUL,
        data_category="availability",
        action="disclose_information",
        purpose="scheduling",
    )
    results = await asyncio.gather(
        policy_service.evaluate(owner_a, request),
        policy_service.evaluate(owner_a, request),
    )
    allowed = [r for r in results if r.decision is PolicyDecision.ALLOW]
    asked = [r for r in results if r.decision is PolicyDecision.ASK]
    assert len(allowed) == 1
    assert len(asked) == 1


async def test_time_limited_consent(policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    await policy_service.create_consent(
        owner_a,
        requester_agent_id=RAHUL,
        data_category="availability",
        action="disclose_information",
        purpose="scheduling",
        decision="ALLOW",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    request = EvaluationRequest(
        requester_agent_id=RAHUL,
        data_category="availability",
        action="disclose_information",
        purpose="scheduling",
    )
    assert (await policy_service.evaluate(owner_a, request)).decision is PolicyDecision.ALLOW


async def test_audit_records_created(policy_service, owner_ids) -> None:
    owner_a, owner_b = owner_ids
    await policy_service.create_policy(
        owner_a,
        requester_agent_id=RAHUL,
        data_category="availability",
        action="disclose_information",
        purpose="scheduling",
        decision="ALLOW",
    )
    await policy_service.evaluate(
        owner_a,
        EvaluationRequest(
            requester_agent_id=RAHUL,
            data_category="availability",
            action="disclose_information",
            purpose="scheduling",
        ),
    )
    await policy_service.evaluate(
        owner_a,
        EvaluationRequest(
            requester_agent_id=PRIYA,
            data_category="financial",
            action="disclose_information",
            purpose="marketing",
        ),
    )

    audit_a = await policy_service.list_decisions(owner_a)
    audit_b = await policy_service.list_decisions(owner_b)
    assert len(audit_a) == 2
    assert len(audit_b) == 0  # owner isolation on the audit trail
    decisions = {r.decision for r in audit_a}
    assert decisions == {"ALLOW", "DENY"}  # financial + no policy -> DENY


async def test_owner_isolation_crud(policy_service, owner_ids) -> None:
    owner_a, owner_b = owner_ids
    policy = await policy_service.create_policy(
        owner_a,
        requester_agent_id=RAHUL,
        data_category="availability",
        action="read_memory",
        purpose="scheduling",
        decision="ALLOW",
    )
    # Owner B sees nothing of A's policies.
    assert await policy_service.list_policies(owner_b) == []
    assert await policy_service.get_policy(owner_b, policy.id) is None
    assert await policy_service.delete_policy(owner_b, policy.id) is False

    consent = await policy_service.create_consent(
        owner_a,
        requester_agent_id=RAHUL,
        data_category="availability",
        action="read_memory",
        purpose="scheduling",
        decision="ALLOW",
    )
    assert await policy_service.list_consents(owner_b) == []
    assert await policy_service.get_consent(owner_b, consent.id) is None
    assert await policy_service.delete_consent(owner_b, consent.id) is False

    # A's requester identity does not bypass B's isolation: B evaluating a
    # request "from Rahul" sees B's own (empty) policy set -> ASK.
    result = await policy_service.evaluate(
        owner_b,
        EvaluationRequest(
            requester_agent_id=RAHUL,
            data_category="availability",
            action="read_memory",
            purpose="scheduling",
        ),
    )
    assert result.decision is PolicyDecision.ASK


# --- Validation / security ----------------------------------------------------------------


async def test_service_rejects_malformed_inputs(policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    bad_cases = [
        {"requester_agent_id": "nexus:ed25519:RAHUL", "data_category": "availability", "action": "read_memory", "purpose": "x"},  # uppercase
        {"requester_agent_id": "nexus:ed25519:abc", "data_category": "drop table owners", "action": "read_memory", "purpose": "x"},
        {"requester_agent_id": "nexus:ed25519:abc", "data_category": "availability", "action": "read'; --", "purpose": "x"},
        {"requester_agent_id": "nexus:ed25519:abc", "data_category": "availability", "action": "read_memory", "purpose": "<script>alert(1)</script>"},
        {"requester_agent_id": "x" * 300, "data_category": "availability", "action": "read_memory", "purpose": "x"},
    ]
    for case in bad_cases:
        with pytest.raises(PolicyServiceError):
            await policy_service.create_policy(owner_a, decision="ALLOW", **case)


async def test_consent_rejects_wildcards(policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    with pytest.raises(PolicyServiceError):
        await policy_service.create_consent(
            owner_a,
            requester_agent_id="*",
            data_category="availability",
            action="read_memory",
            purpose="scheduling",
            decision="ALLOW",
        )


async def test_invalid_decision_rejected(policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    with pytest.raises(PolicyServiceError):
        await policy_service.create_policy(
            owner_a,
            requester_agent_id=RAHUL,
            data_category="availability",
            action="read_memory",
            purpose="scheduling",
            decision="MAYBE",
        )


async def test_invalid_scope_rejected(policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    with pytest.raises(PolicyServiceError):
        await policy_service.create_policy(
            owner_a,
            requester_agent_id=RAHUL,
            data_category="availability",
            action="read_memory",
            purpose="scheduling",
            decision="ALLOW",
            disclosure_scope="everything",
        )


async def test_expiry_before_start_rejected(policy_service, owner_ids) -> None:
    owner_a, _ = owner_ids
    with pytest.raises(PolicyServiceError):
        await policy_service.create_policy(
            owner_a,
            requester_agent_id=RAHUL,
            data_category="availability",
            action="read_memory",
            purpose="scheduling",
            decision="ALLOW",
            starts_at=NOW + timedelta(days=2),
            expires_at=NOW + timedelta(days=1),
        )


async def test_unknown_but_wellformed_category_allowed(policy_service, owner_ids) -> None:
    """Future categories must work without code changes (spec §5)."""
    owner_a, _ = owner_ids
    await policy_service.create_policy(
        owner_a,
        requester_agent_id=RAHUL,
        data_category="medical",
        action="disclose_information",
        purpose="health",
        decision="DENY",
    )
    result = await policy_service.evaluate(
        owner_a,
        EvaluationRequest(
            requester_agent_id=RAHUL,
            data_category="medical",
            action="disclose_information",
            purpose="health",
        ),
    )
    assert result.decision is PolicyDecision.DENY
