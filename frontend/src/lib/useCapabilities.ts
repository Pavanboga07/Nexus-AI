"use client";

import { useCallback, useEffect, useState } from "react";

import { CapabilityInfo, getCapabilities } from "./api/identity";

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
  const [capabilities, setCapabilities] = useState<CapabilityInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    setLoading(true);
    setError(null);
    void getCapabilities()
      .then((res) => {
        setCapabilities(res.capabilities ?? []);
        setLoading(false);
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : "Could not load capabilities.");
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return { capabilities, loading, error, reload };
}
