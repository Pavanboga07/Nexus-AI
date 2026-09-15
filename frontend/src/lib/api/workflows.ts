import { apiFetch } from "./client";
import { WorkflowOut } from "@/types/api";

export async function listWorkflows(status?: string): Promise<{
  workflows: WorkflowOut[];
  total: number;
}> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiFetch<{ workflows: WorkflowOut[]; total: number }>(
    `/workflows${query}`
  );
}

export async function getWorkflow(workflowId: string): Promise<WorkflowOut> {
  return apiFetch<WorkflowOut>(`/workflows/${workflowId}`);
}

export async function createWorkflow(params: {
  workflow_type: string;
  purpose: string;
  steps: Array<{
    step_type: string;
    input_payload?: Record<string, unknown>;
    max_attempts?: number;
  }>;
  context_data?: Record<string, unknown>;
  ttl_seconds?: number;
}): Promise<WorkflowOut> {
  return apiFetch<WorkflowOut>("/workflows", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

export async function startWorkflow(
  workflowId: string
): Promise<{ workflow_id: string; status: string }> {
  return apiFetch(`/workflows/${workflowId}/start`, {
    method: "POST",
  });
}

export async function approveWorkflow(
  workflowId: string,
  stepId?: string
): Promise<{ workflow_id: string; status: string }> {
  return apiFetch(`/workflows/${workflowId}/approve`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId }),
  });
}

export async function cancelWorkflow(
  workflowId: string,
  reason: string = "Cancelled by user"
): Promise<{ workflow_id: string; status: string }> {
  return apiFetch(`/workflows/${workflowId}/cancel`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}
