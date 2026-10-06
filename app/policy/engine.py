"""The deterministic policy engine (Part 4 core).

Evaluates a structured request against stored policies and consents and
returns ALLOW / ASK / DENY plus a disclosure scope. Pure decision logic:

- NO SQL (repository supplies candidates)
- NO LLM, embeddings, or memory similarity
- Same inputs + same database state => same decision, always

Precedence (most specific first; documented in README):

    1. exact requester + exact data + exact action + exact purpose
    2. exact requester + exact data + exact action
    3. exact requester + exact data
    4. exact requester
    5. wildcard requester + exact data + exact action + exact purpose
    6. wildcard requester + exact data + exact action
    7. wildcard requester + exact data
    8. global default (all wildcards)

Tie-breaking within one specificity level:
    - higher ``priority`` wins
    - equal priority: DENY > ASK > ALLOW  (never let a broad ALLOW override
      an explicit DENY, spec §12)

High-sensitivity categories (financial, private) are deny-by-default: a
wildcard-category rule never satisfies them; only exact-category rules or
explicit consents do (spec §11).

No rule at all -> ASK (secure default), except sensitive categories -> DENY.
Consents are checked BEFORE policies: an explicit owner approval/denial is
the most personal statement of intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.observability import POLICY_DECISIONS
from app.policy.models import (
    SENSITIVE_DEFAULT_DENY,
    WILDCARD,
    Consent,
    DisclosureScope,
    Policy,
    PolicyDecision,
)

#: Specificity levels, most specific first. Index encodes precedence.
_SPECIFICITY_LEVELS: tuple[tuple[bool, bool, bool, bool], ...] = (
    (True, True, True, True),    # 1: requester+data+action+purpose
    (True, True, True, False),   # 2: requester+data+action
    (True, True, False, False),  # 3: requester+data
    (True, False, False, False), # 4: requester only
    (False, True, True, True),   # 5: wildcard requester+data+action+purpose
    (False, True, True, False),  # 6: wildcard requester+data+action
    (False, True, False, False), # 7: wildcard requester+data
    (False, False, False, False),# 8: global default
)

_DECISION_RANK = {
    PolicyDecision.DENY: 2,
    PolicyDecision.ASK: 1,
    PolicyDecision.ALLOW: 0,
}


@dataclass(frozen=True)
class EvaluationRequest:
    """Structured authorization question. Concrete values only - a request
    may never carry the wildcard (that would be asking 'is anyone allowed',
    which is not a question the engine answers)."""

    requester_agent_id: str
    data_category: str
    action: str
    purpose: str
    resource_id: str | None = None


@dataclass(frozen=True)
class EvaluationResult:
    decision: PolicyDecision
    reason: str
    matched_policy_id: str | None = None
    matched_consent_id: str | None = None
    requires_user_approval: bool = False
    disclosure_scope: DisclosureScope | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "matched_policy_id": self.matched_policy_id,
            "matched_consent_id": self.matched_consent_id,
            "requires_user_approval": self.requires_user_approval,
            "disclosure_scope": (
                self.disclosure_scope.value if self.disclosure_scope else None
            ),
        }


def _rule_is_active(rule: Policy | Consent, now: datetime) -> bool:
    """Expired rules never match; rules with a future start are not yet
    active (spec §12)."""
    if rule.expires_at is not None and rule.expires_at <= now:
        return False
    starts_at = getattr(rule, "starts_at", None)
    if starts_at is not None and starts_at > now:
        return False
    return True


def _specificity(
    rule: Policy, request: EvaluationRequest
) -> tuple[int, int, int]:
    """Return (level, priority, decision_rank) for sorting.

    Lower level = more specific = higher precedence.
    """
    exact_requester = rule.requester_agent_id == request.requester_agent_id
    exact_data = rule.data_category == request.data_category
    exact_action = rule.action == request.action
    exact_purpose = rule.purpose == request.purpose
    signature = (exact_requester, exact_data, exact_action, exact_purpose)
    try:
        level = _SPECIFICITY_LEVELS.index(signature)
    except ValueError:
        # A rule with e.g. exact action but wildcard requester+data does not
        # fit the documented ladder; it still participates, ranked just
        # below the ladder levels but above nothing. Deterministic.
        level = len(_SPECIFICITY_LEVELS)
    return level, -rule.priority, -_DECISION_RANK[PolicyDecision(rule.decision)]


class PolicyEngine:
    """Pure evaluation over in-memory rule sets."""

    def evaluate(
        self,
        request: EvaluationRequest,
        *,
        policies: list[Policy],
        consents: list[Consent],
        now: datetime | None = None,
    ) -> EvaluationResult:
        """Evaluate, and count the outcome (M11).

        The metric is recorded here rather than in the caller because this is
        the single point every decision passes through - the service, the A2A
        pipeline and the autonomy engine all reach the engine this way, so one
        call site covers all of them and none can forget.

        ASK and DENY are separated in the labels specifically so an alert can
        fire on "denials spiked" or "everything is asking for approval", which
        are the two failure modes a policy engine actually has.
        """
        result = self._evaluate(request, policies=policies, consents=consents, now=now)
        POLICY_DECISIONS.inc(1, decision=result.decision.value)
        return result

    def _evaluate(
        self,
        request: EvaluationRequest,
        *,
        policies: list[Policy],
        consents: list[Consent],
        now: datetime | None = None,
    ) -> EvaluationResult:
        now = now or datetime.now(UTC)

        # 1. Consents: the owner's explicit, most recent intent. Exact match
        #    on all four dimensions (consents never use wildcards).
        consent = self._matching_consent(request, consents, now)
        if consent is not None:
            if consent.decision == PolicyDecision.DENY.value:
                return EvaluationResult(
                    decision=PolicyDecision.DENY,
                    reason="Owner explicitly denied this request (consent)",
                    matched_consent_id=str(consent.id),
                    requires_user_approval=False,
                    disclosure_scope=DisclosureScope.NONE,
                )
            return EvaluationResult(
                decision=PolicyDecision.ALLOW,
                reason="Owner-approved consent matches this request",
                matched_consent_id=str(consent.id),
                requires_user_approval=False,
                disclosure_scope=DisclosureScope(consent.disclosure_scope),
            )

        # 2. Policies: most specific active rule wins, with DENY-first tie
        #    breaking inside a specificity level.
        applicable = [
            rule
            for rule in policies
            if _rule_is_active(rule, now)
            and self._rule_matches(rule, request)
        ]
        if applicable:
            best = min(applicable, key=lambda rule: _specificity(rule, request))
            decision = PolicyDecision(best.decision)
            return EvaluationResult(
                decision=decision,
                reason=f"Matched policy ({self._describe(best, request)})",
                matched_policy_id=str(best.id),
                requires_user_approval=decision == PolicyDecision.ASK,
                disclosure_scope=DisclosureScope(best.disclosure_scope),
            )

        # 3. Secure default.
        if request.data_category in SENSITIVE_DEFAULT_DENY:
            return EvaluationResult(
                decision=PolicyDecision.DENY,
                reason=(
                    f"'{request.data_category}' is deny-by-default: it "
                    "requires an explicit policy or consent"
                ),
                requires_user_approval=False,
                disclosure_scope=None,
            )
        return EvaluationResult(
            decision=PolicyDecision.ASK,
            reason="No applicable permission exists",
            requires_user_approval=True,
            disclosure_scope=None,
        )

    def _matching_consent(
        self,
        request: EvaluationRequest,
        consents: list[Consent],
        now: datetime,
    ) -> Consent | None:
        """Newest valid, unconsumed, exactly-matching consent."""
        matches = [
            consent
            for consent in consents
            if _rule_is_active(consent, now)
            and consent.requester_agent_id == request.requester_agent_id
            and consent.data_category == request.data_category
            and consent.action == request.action
            and consent.purpose == request.purpose
            and consent.used_at is None
        ]
        return max(matches, key=lambda c: c.created_at, default=None)

    def _rule_matches(self, rule: Policy, request: EvaluationRequest) -> bool:
        if rule.requester_agent_id not in (WILDCARD, request.requester_agent_id):
            return False
        if rule.data_category not in (WILDCARD, request.data_category):
            return False
        # Purpose limitation: an exact-purpose rule only answers the exact
        # purpose; the wildcard covers "any purpose". A scheduling rule must
        # never leak into marketing (spec §13).
        if rule.purpose not in (WILDCARD, request.purpose):
            return False
        if rule.action not in (WILDCARD, request.action):
            return False
        # Sensitive categories need an exact data_category match - a
        # wildcard-category ALLOW must not unlock financial/private data.
        if (
            request.data_category in SENSITIVE_DEFAULT_DENY
            and rule.data_category == WILDCARD
        ):
            return False
        return True

    @staticmethod
    def _describe(rule: Policy, request: EvaluationRequest) -> str:
        parts = []
        parts.append(
            "exact requester"
            if rule.requester_agent_id == request.requester_agent_id
            else "wildcard requester"
        )
        parts.append(
            f"data={rule.data_category}, action={rule.action}, purpose={rule.purpose}"
        )
        return ", ".join(parts)


__all__ = [
    "EvaluationRequest",
    "EvaluationResult",
    "PolicyEngine",
]
