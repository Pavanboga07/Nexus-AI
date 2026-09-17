/**
 * Trusted (remote) agents.
 *
 * Shapes here are the ones the API actually returns. Two of them were wrong
 * before: `TrustedAgent` advertised `public_key_b64` and `endpoint_url`, but
 * `TrustedAgentOut` has neither (`public_key` and `endpoint`) - so anything
 * reading them got `undefined` with no error. And the audit endpoint returns
 * `messages`, not `entries`.
 */

import { apiFetch } from "./client";

export type TrustedAgent = {
  agent_id: string;
  display_name: string;
  /** The peer's A2A endpoint. Named `endpoint`, not `endpoint_url`. */
  endpoint: string;
  status: string;
  created_at?: string | null;
};

export type TrustedAgentList = {
  agents: TrustedAgent[];
  total: number;
};

export type A2AAuditEntry = {
  id: string;
  message_id: string;
  task_id: string;
  sender_agent_id: string;
  recipient_agent_id: string;
  message_type: string;
  purpose: string;
  policy_decision?: string | null;
  status: string;
  error_code?: string | null;
  created_at?: string | null;
  processed_at?: string | null;
};

export async function listTrustedAgents(): Promise<TrustedAgentList> {
  return apiFetch<TrustedAgentList>("/a2a/agents");
}

export async function registerTrustedAgent(params: {
  agent_id: string;
  display_name: string;
  public_key: string;
  endpoint: string;
}): Promise<TrustedAgent> {
  return apiFetch<TrustedAgent>("/a2a/agents", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

/**
 * Revoke trust. Revocation is a POST (the record is kept so the decision is
 * auditable); DELETE removes the record entirely.
 */
export async function revokeTrustedAgent(agentId: string): Promise<TrustedAgent> {
  return apiFetch<TrustedAgent>(
    `/a2a/agents/${encodeURIComponent(agentId)}/revoke`,
    { method: "POST" }
  );
}

export async function deleteTrustedAgent(
  agentId: string
): Promise<{ deleted: boolean; agent_id: string | null }> {
  return apiFetch(`/a2a/agents/${encodeURIComponent(agentId)}`, {
    method: "DELETE",
  });
}

/**
 * The A2A audit trail (metadata only - the backend never stores content).
 */
export async function getA2AAudit(limit = 100): Promise<{
  messages: A2AAuditEntry[];
  total: number;
}> {
  return apiFetch<{ messages: A2AAuditEntry[]; total: number }>(
    `/a2a/audit/list?limit=${limit}`
  );
}

/**
 * Send a request to a trusted agent.
 *
 * `purpose` and `data_category` are policy dimensions: the receiving agent
 * evaluates them before disclosing anything, so they are not decoration.
 */
export async function sendToAgent(params: {
  recipient_agent_id: string;
  purpose: string;
  action: string;
  data_category: string;
  payload?: Record<string, unknown>;
  endpoint?: string;
}): Promise<{ task_id: string; recipient: string; status: string; payload?: unknown }> {
  return apiFetch("/a2a/send", {
    method: "POST",
    body: JSON.stringify({
      recipient_agent_id: params.recipient_agent_id,
      purpose: params.purpose,
      action: params.action,
      data_category: params.data_category,
      payload: params.payload ?? {},
      endpoint: params.endpoint ?? null,
    }),
  });
}
