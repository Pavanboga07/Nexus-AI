import { apiFetch } from "./client";
import { AgentCard, PublicIdentity } from "@/types/api";

export type IdentityInfo = PublicIdentity & { key_algorithm: string };

/**
 * The deployment's own public identity.
 *
 * `IdentityResponse` is exactly `{agent_id, public_key, key_algorithm,
 * fingerprint}` - there is no separate base64 field. This used to add
 * `public_key_b64: data.public_key`, a second name for the same value that made
 * it look as though the response carried two keys.
 */
export async function getIdentity(): Promise<IdentityInfo> {
  return apiFetch<IdentityInfo>("/identity");
}

export async function verifySignature(params: {
  agent_id: string;
  message: string;
  signature: string;
  public_key: string;
}): Promise<{ valid: boolean; agent_id_matches: boolean; reason?: string | null }> {
  // The response also reports whether the public key actually derives the
  // claimed agent_id, which is the part that stops a peer from presenting
  // someone else's key. It used to be typed as `error`, so it was dropped.
  return apiFetch<{
    valid: boolean;
    agent_id_matches: boolean;
    reason?: string | null;
  }>("/identity/verify", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

export async function getAgentCard(): Promise<AgentCard> {
  return apiFetch<AgentCard>("/a2a/card");
}

/**
 * One typed capability contract from the live registry
 * (`CapabilityOut` — `app/schemas/identity.py`).
 *
 * Field names are the wire names verbatim, so a backend rename shows up as a
 * TypeScript error rather than a blank panel.
 */
export type CapabilityInfo = {
  id: string;
  version: string;
  description: string;
  data_category: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
};

/**
 * The live capability registry: what this agent will actually accept on a
 * 0.2 envelope. Rendered by the Agent page's "What your agent can do" panel.
 */
export async function getCapabilities(): Promise<{
  capabilities: CapabilityInfo[];
  total: number;
}> {
  return apiFetch<{ capabilities: CapabilityInfo[]; total: number }>(
    "/identity/capabilities"
  );
}
