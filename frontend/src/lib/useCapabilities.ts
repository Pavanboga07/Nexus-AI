"use client";

import { CapabilityInfo, getCapabilities } from "./api/identity";
import { useAsync } from "./useAsync";

/**
 * The live capability registry as picker options.
 *
 * One fetcher for every "ask a person" surface (Tasks delegate modal, Chat
 * ask modal) so both offer exactly what the agent will accept — never a
 * hardcoded list that drifts from the backend.
 */
export function useCapabilities(): {
  capabilities: CapabilityInfo[];
  loading: boolean;
  error: string | null;
  reload: () => void;
} {
  const { data, loading, error, reload } = useAsync(getCapabilities);

  return { capabilities: data?.capabilities ?? [], loading, error, reload };
}
