import { apiFetch } from "./client";
import {
  Consent,
  PolicyDecision,
  PolicyEvaluateRequest,
  PolicyEvaluateResponse,
  PolicyRule,
} from "@/types/api";

export async function listPolicies(): Promise<{
  policies: PolicyRule[];
  total: number;
}> {
  const res = await apiFetch<{ policies: any[]; total: number }>("/policy");
  return {
    policies: (res.policies || []).map((p) => ({
      id: p.id,
      rule_type: p.data_category || "explicit",
      action: p.action,
      resource: p.purpose || "*",
      decision: p.decision,
      priority: p.priority,
    })),
    total: res.total,
  };
}

export async function listConsents(): Promise<{
  consents: Consent[];
  total: number;
}> {
  const res = await apiFetch<{ consents: any[]; total: number }>("/consent");
  return {
    consents: (res.consents || []).map((c) => ({
      consent_id: c.id,
      owner_id: c.requester_agent_id,
      action: c.action,
      resource: c.purpose,
      single_use: false,
      used: false,
      expires_at: c.expires_at || "",
      created_at: c.expires_at || "",
    })),
    total: res.total,
  };
}

export async function evaluatePolicy(
  payload: PolicyEvaluateRequest
): Promise<PolicyEvaluateResponse> {
  const actionParts = payload.action.split(":");
  const body = {
    requester_agent_id: payload.peer_agent_id || "self",
    data_category: actionParts[0] || "tool",
    action: actionParts[1] || payload.action,
    purpose: "user-evaluation",
    resource_id: payload.resource,
  };
  const res = await apiFetch<any>("/policy/evaluate", {
    method: "POST",
    body: JSON.stringify(body),
  });

  return {
    decision: res.decision,
    reason: res.reason,
    rule_id: res.matched_policy_id || res.matched_consent_id,
  };
}

export async function getPolicyAudit(): Promise<{
  audits: Array<{
    id: string;
    owner_id?: string;
    timestamp: string;
    action: string;
    resource: string;
    decision: PolicyDecision;
    reason: string;
    matched_rule_id?: string;
    peer_agent_id?: string;
  }>;
  total: number;
}> {
  const res = await apiFetch<{ decisions: any[]; total: number }>(
    "/policy/audit"
  );
  return {
    audits: (res.decisions || []).map((d) => ({
      id: d.id,
      timestamp: d.created_at || new Date().toISOString(),
      action: `${d.data_category}:${d.action}`,
      resource: d.purpose,
      decision: d.decision,
      reason: d.reason,
      matched_rule_id: d.matched_policy_id || d.matched_consent_id,
      peer_agent_id: d.requester_agent_id,
    })),
    total: res.total,
  };
}
