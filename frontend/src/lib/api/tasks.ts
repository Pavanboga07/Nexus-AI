import { apiFetch } from "./client";
import { A2ATask } from "@/types/api";

export async function listTasks(status?: string): Promise<{
  tasks: A2ATask[];
  total: number;
}> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiFetch<{ tasks: A2ATask[]; total: number }>(`/a2a/tasks${query}`);
}

export async function getTask(taskId: string): Promise<A2ATask> {
  return apiFetch<A2ATask>(`/a2a/tasks/${taskId}`);
}

export async function delegateTask(params: {
  recipient_agent_id: string;
  task_type: string;
  purpose: string;
  payload: Record<string, unknown>;
  endpoint?: string;
}): Promise<{ task_id: string; status: string; detail?: string }> {
  // Ensure purpose is a valid slug format (lowercase, hyphens/underscores)
  const slugPurpose = params.purpose
    .toLowerCase()
    .replace(/[^a-z0-9_\-.]/g, "-")
    .replace(/-+/g, "-") || "delegated-task";

  return apiFetch("/a2a/tasks", {
    method: "POST",
    body: JSON.stringify({
      recipient_agent_id: params.recipient_agent_id,
      task_type: params.task_type,
      purpose: slugPurpose,
      payload: params.payload,
      endpoint: params.endpoint,
    }),
  });
}

export async function approveTask(
  taskId: string,
  notes: string = "Approved by owner"
): Promise<{ task_id: string; status: string }> {
  return apiFetch(`/a2a/tasks/${taskId}/approve`, {
    method: "POST",
    body: JSON.stringify({ notes }),
  });
}

export async function rejectTask(
  taskId: string,
  reason: string = "Declined by owner"
): Promise<{ task_id: string; status: string }> {
  return apiFetch(`/a2a/tasks/${taskId}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export async function cancelTask(
  taskId: string
): Promise<{ task_id: string; status: string }> {
  return apiFetch(`/a2a/tasks/${taskId}/cancel`, {
    method: "POST",
  });
}

export async function negotiateTask(
  taskId: string,
  params: {
    proposal_payload: Record<string, unknown>;
    purpose?: string;
  }
): Promise<{ task_id: string; status: string; detail?: string }> {
  return apiFetch(`/a2a/tasks/${taskId}/negotiate`, {
    method: "POST",
    body: JSON.stringify({
      proposal_payload: params.proposal_payload,
      purpose: params.purpose,
    }),
  });
}
