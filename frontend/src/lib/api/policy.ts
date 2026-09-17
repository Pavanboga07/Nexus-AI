import { apiFetch } from "./client";
import {
  Consent,
  PolicyDecision,
  PolicyEvaluateRequest,
  PolicyEvaluateResponse,
  PolicyRule,
} from "@/types/api";

/**
 * Policy rules, consents and the decision audit.
 *
 * These three functions used to return hand-rolled objects - renaming
 * `data_category` to `rule_type`, `purpose` to `resource`, and hardcoding
 * `single_use: false` - so the pages rendered plausible-looking values that the
 * backend never sent. The API's own shapes are used verbatim now: a field that
 * does not exist is a TypeScript error rather than a confident guess.
 */
export async function listPolicies(): Promise<{
  policies: PolicyRule[];
  total: number;
}> {
  return apiFetch<{ policies: PolicyRule[]; total: number }>("/policy");
}

export async function listConsents(): Promise<{
  consents: Consent[];
  total: number;
}> {
  return apiFetch<{ consents: Consent[]; total: number }>("/consent");
}

export async function evaluatePolicy(
  payload: PolicyEvaluateRequest
): Promise<PolicyEvaluateResponse> {
  const actionParts = payload.action.split(":");
  const body = {
    requester_agent_id: payload.peer_agent_id || "self",
    data_category: actionParts[0] || "tool",
    action: actionParts[1] || payload.action,
    purpose: payload.purpose || "user-evaluation",
    resource_id: payload.resource,
  };
  const res = await apiFetch<{
    decision: PolicyDecision;
    reason: string;
    matched_policy_id?: string | null;
    matched_consent_id?: string | null;
    requires_user_approval: boolean;
    disclosure_scope?: PolicyRule["disclosure_scope"] | null;
  }>("/policy/evaluate", {
    method: "POST",
    body: JSON.stringify(body),
  });

  return {
    decision: res.decision,
    reason: res.reason,
    rule_id: res.matched_policy_id || res.matched_consent_id || undefined,
    requires_user_approval: res.requires_user_approval,
    disclosure_scope: res.disclosure_scope ?? undefined,
  };
}

export type PolicyAuditEntry = {
  id: string;
  requester_agent_id: string;
  data_category: string;
  action: string;
  purpose: string;
  decision: PolicyDecision;
  reason: string;
  matched_policy_id?: string | null;
  matched_consent_id?: string | null;
  created_at?: string | null;
};

export async function getPolicyAudit(): Promise<{
  decisions: PolicyAuditEntry[];
  total: number;
}> {
  return apiFetch<{ decisions: PolicyAuditEntry[]; total: number }>(
    "/policy/audit"
  );
}
