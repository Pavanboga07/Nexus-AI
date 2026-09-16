import { apiFetch } from "./client";
import { AgentCard, TrustedAgent } from "@/types/api";

export async function listTrustedAgents(): Promise<{
  agents: TrustedAgent[];
  total: number;
}> {
  return apiFetch<{ agents: TrustedAgent[]; total: number }>("/a2a/agents");
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

export async function revokeTrustedAgent(
  agentId: string
): Promise<TrustedAgent> {
  return apiFetch<TrustedAgent>(`/a2a/agents/${agentId}/revoke`, {
    method: "POST",
  });
}

export async function discoverRemoteAgent(
  url: string,
  displayName?: string
): Promise<{
  card: AgentCard;
  verified: boolean;
  registered: boolean;
  trusted_agent?: TrustedAgent;
}> {
  const res = await apiFetch<any>("/a2a/discover", {
    method: "POST",
    body: JSON.stringify({
      url,
      display_name: displayName,
    }),
  });

  return {
    card: {
      agent_id: res.card.agent_id,
      name: res.card.display_name,
      description: res.card.protocol,
      version: res.card.version,
      endpoints: { a2a: res.card.endpoint },
      capabilities: (res.card.capabilities || []).map((c: any) =>
        typeof c === "string" ? c : c.name
      ),
      public_key: res.card.public_key,
      signature: res.card.signature,
      created_at: res.card.issued_at,
      expires_at: res.card.expires_at,
    },
    verified: true,
    registered: true,
    trusted_agent: {
      agent_id: res.agent_id,
      display_name: res.display_name,
      public_key_b64: res.card.public_key,
      endpoint_url: res.endpoint,
      capabilities: (res.card.capabilities || []).map((c: any) =>
        typeof c === "string" ? c : c.name
      ),
      status: res.status,
      created_at: res.card.issued_at,
      updated_at: res.card.issued_at,
    },
  };
}

export async function getA2AAudit(): Promise<{
  entries: Array<{
    id: string;
    sender_id: string;
    recipient_id: string;
    message_type: string;
    direction: "inbound" | "outbound";
    status: string;
    error?: string;
    timestamp: string;
  }>;
  total: number;
}> {
  return apiFetch("/a2a/audit/list");
}

export async function searchGatewayDirectory(q: string): Promise<{
  agents: Array<{
    agent_id: string;
    display_name: string;
    handle?: string;
    public_key: string;
    endpoint: string;
    capabilities: string[];
    is_online: boolean;
    verified: boolean;
    is_trusted: boolean;
    card?: any;
  }>;
  total: number;
}> {
  return apiFetch(`/a2a/directory/search?q=${encodeURIComponent(q)}`);
}
