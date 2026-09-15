"use client";

import React, { useEffect, useState } from "react";
import { getHealth } from "@/lib/api/system";
import { HealthResponse } from "@/types/api";

export function StatusDot() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    const fetch = async () => {
      try {
        const data = await getHealth();
        setHealth(data);
        setError(false);
      } catch {
        setError(true);
        setHealth(null);
      }
    };
    fetch();
    const interval = setInterval(fetch, 15000);
    return () => clearInterval(interval);
  }, []);

  const color = error
    ? "bg-red-500"
    : !health
    ? "bg-neutral-500"
    : !health.database || !health.identity
    ? "bg-amber-500"
    : "bg-emerald-500";

  const label = error
    ? "Offline"
    : !health
    ? "Connecting"
    : !health.database || !health.identity
    ? "Degraded"
    : "Online";

  return (
    <div className="flex items-center gap-2 text-xs text-neutral-500" title={label}>
      <span className={`w-1.5 h-1.5 rounded-full ${color}`} />
      <span>{label}</span>
    </div>
  );
}
