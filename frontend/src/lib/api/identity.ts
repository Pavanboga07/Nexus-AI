import { apiFetch } from "./client";
import { AgentCard, PublicIdentity } from "@/types/api";

export type IdentityInfo = PublicIdentity & {
  key_algorithm: string;
  public_key: string;
};

export async function getIdentity(): Promise<IdentityInfo> {
  const data = await apiFetch<{
    agent_id: string;
    public_key: string;
    key_algorithm: string;
    fingerprint: string;
  }>("/identity");
  return {
    ...data,
    public_key_b64: data.public_key,
  };
}

export async function verifySignature(params: {
  agent_id: string;
  message: string;
  signature: string;
  public_key: string;
}): Promise<{ valid: boolean; error?: string }> {
  return apiFetch<{ valid: boolean; error?: string }>("/identity/verify", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

export async function getAgentCard(): Promise<AgentCard> {
  return apiFetch<AgentCard>("/a2a/card");
}
