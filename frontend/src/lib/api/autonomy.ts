import { apiFetch } from "./client";
import {
  AutonomyConfigOut,
  AutonomyRunOut,
  AutonomyDecisionOut,
} from "@/types/api";

export async function getAutonomyConfig(): Promise<AutonomyConfigOut> {
  return apiFetch<AutonomyConfigOut>("/autonomy/config");
}

export async function updateAutonomyConfig(
  params: Partial<AutonomyConfigOut>
): Promise<AutonomyConfigOut> {
  return apiFetch<AutonomyConfigOut>("/autonomy/config", {
    method: "PATCH",
    body: JSON.stringify(params),
  });
}

export async function listAutonomyRuns(status?: string): Promise<{
  runs: AutonomyRunOut[];
  total: number;
}> {
  const query = status && status !== "all" ? `?status=${encodeURIComponent(status)}` : "";
  return apiFetch<{ runs: AutonomyRunOut[]; total: number }>(
    `/autonomy/runs${query}`
  );
}

export async function getAutonomyRun(runId: string): Promise<AutonomyRunOut> {
  return apiFetch<AutonomyRunOut>(`/autonomy/runs/${runId}`);
}

export async function createAutonomyRun(params: {
  goal: string;
  context_data?: Record<string, unknown>;
  execute_immediately?: boolean;
}): Promise<AutonomyRunOut> {
  return apiFetch<AutonomyRunOut>("/autonomy/runs", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

export async function approveAutonomyRun(
  runId: string,
  notes?: string
): Promise<AutonomyRunOut> {
  return apiFetch<AutonomyRunOut>(`/autonomy/runs/${runId}/approve`, {
    method: "POST",
    body: JSON.stringify({ notes }),
  });
}

export async function rejectAutonomyRun(
  runId: string,
  notes?: string
): Promise<AutonomyRunOut> {
  return apiFetch<AutonomyRunOut>(`/autonomy/runs/${runId}/reject`, {
    method: "POST",
    body: JSON.stringify({ notes }),
  });
}

export async function cancelAutonomyRun(
  runId: string,
  reason?: string
): Promise<AutonomyRunOut> {
  return apiFetch<AutonomyRunOut>(`/autonomy/runs/${runId}/cancel`, {
    method: "POST",
    body: JSON.stringify({ reason: reason || "Cancelled by user" }),
  });
}

export async function listAutonomyDecisions(
  runId: string
): Promise<{ decisions: AutonomyDecisionOut[]; total: number }> {
  return apiFetch<{ decisions: AutonomyDecisionOut[]; total: number }>(
    `/autonomy/runs/${runId}/decisions`
  );
}
