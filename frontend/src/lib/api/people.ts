/**
 * People (remote agents) - the owner's view of who their agent can talk to.
 *
 * Two things this page deliberately gets right, because the backend audit
 * found the UI could otherwise present a lie:
 *
 * 1. **"Verified" means the card signature verified.** The directory endpoint
 *    returns `verified` plus `verification_error`, and only exposes `card` when
 *    verification passed. The UI shows a badge ONLY on `verified === true`, and
 *    otherwise says why - never "unverified but probably fine".
 *
 * 2. **Discovery and trust are separate steps.** Finding an agent in the
 *    directory does not trust it. The user must explicitly connect, which is
 *    what pins the key. A list that auto-trusted everything it displayed would
 *    make the trust decision for the user.
 */

import { apiFetch } from "./client";

export type DirectoryAgent = {
  agent_id: string;
  display_name: string;
  handle?: string | null;
  public_key?: string | null;
  endpoint?: string | null;
  capabilities: string[];
  is_online: boolean;
  /** True ONLY when the agent card's Ed25519 signature verified. */
  verified: boolean;
  /** Why verification failed, when it did. */
  verification_error?: string | null;
  is_trusted: boolean;
  card?: Record<string, unknown> | null;
};

export type TrustedAgent = {
  agent_id: string;
  display_name: string;
  endpoint: string;
  status: string;
  created_at?: string | null;
};

export async function listTrusted(): Promise<TrustedAgent[]> {
  const data = await apiFetch<{ agents: TrustedAgent[] }>("/a2a/agents");
  return data.agents ?? [];
}

export async function searchDirectory(query: string): Promise<DirectoryAgent[]> {
  if (!query.trim()) return [];
  const data = await apiFetch<{ agents: DirectoryAgent[] }>(
    `/a2a/directory/search?q=${encodeURIComponent(query)}`
  );
  return data.agents ?? [];
}

/**
 * Where to fetch a directory result's card for the connect flow.
 *
 * The directory payload carries no card URL — only the endpoint from the
 * (verified) card — so the card is fetched from the well-known path on the
 * endpoint's origin, the same path this deployment serves its own card from.
 * Returns null when there is no endpoint to derive from.
 */
export function cardUrlForResult(agent: DirectoryAgent): string | null {
  if (!agent.endpoint) return null;
  try {
    return `${new URL(agent.endpoint).origin}/.well-known/nexus-agent.json`;
  } catch {
    return null;
  }
}

/**
 * Connect to an agent by card URL.
 *
 * The backend fetches the card and verifies its signature, agent_id/key
 * binding and time window BEFORE registering it. This is the only path that
 * creates trust, and it cannot be reached with unverified data.
 */
export async function connectByCardUrl(
  url: string,
  displayName?: string
): Promise<{ agent_id: string; display_name: string; endpoint: string }> {
  return apiFetch("/a2a/discover", {
    method: "POST",
    body: JSON.stringify({ url, display_name: displayName ?? null }),
  });
}

export async function revokeTrust(agentId: string): Promise<void> {
  await apiFetch(`/a2a/agents/${encodeURIComponent(agentId)}/revoke`, {
    method: "POST",
  });
}

export async function removeTrust(agentId: string): Promise<void> {
  await apiFetch(`/a2a/agents/${encodeURIComponent(agentId)}`, {
    method: "DELETE",
  });
}

/** The local agent's own identity, so it can be shared with others. */
export async function getLocalIdentity(): Promise<{
  agent_id: string;
  public_key: string;
  fingerprint: string;
}> {
  return apiFetch("/identity");
}
