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
  // Ensure purpose is a valid slug format (lowercase, hyphens/underscores).
  // This is a subset of the backend PURPOSE_PATTERN (`^[a-z0-9:_-.]{1,64}$`
  // in app/a2a/schemas.py, enforced on TaskDelegateRequest in
  // app/schemas/tasks.py), so anything this produces validates. Length is
  // capped by the inputs (purpose fields use maxLength={64}).
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

/**
 * Capability id -> task_type the delegate endpoint accepts.
 *
 * These are two separate backend vocabularies: the capability registry
 * (`/identity/capabilities`, ids like `calendar.availability`) describes what
 * an agent advertises, while the delegate endpoint only accepts registered
 * task-handler names (`availability_check`, `meeting_proposal`,
 * `information_request` in app/a2a/handlers.py) and 400s anything else. The
 * backend registers the capabilities as mirrors of the handlers
 * (A2AService._register_default_capabilities in app/a2a/service.py), so this
 * table mirrors that pairing. A capability with no entry here cannot be sent
 * as a task; the pickers disable submit rather than send a doomed request.
 */
export const CAPABILITY_TASK_TYPES: Record<string, string> = {
  "calendar.availability": "availability_check",
  "calendar.propose_meeting": "meeting_proposal",
  "information.request": "information_request",
};

/**
 * The payload key each task handler requires (BaseTaskHandler.validate in
 * app/a2a/handlers.py). Used to shape a free-text summary into a valid
 * payload and to prefill the JSON field — never sent blindly, the server
 * still validates.
 */
export const TASK_REQUIRED_PAYLOAD_KEY: Record<string, string> = {
  availability_check: "requested_time",
  meeting_proposal: "proposed_time",
  information_request: "query",
};

/** Shape a plain-text summary into the payload its handler requires. */
export function payloadForTaskType(
  taskType: string,
  summary: string
): Record<string, unknown> {
  const key = TASK_REQUIRED_PAYLOAD_KEY[taskType];
  return key ? { [key]: summary } : { message: summary };
}

/** Empty JSON template prefilled when a capability is picked. */
export function payloadTemplateForTaskType(taskType: string): string {
  const key = TASK_REQUIRED_PAYLOAD_KEY[taskType];
  return JSON.stringify(key ? { [key]: "" } : {}, null, 2);
}
