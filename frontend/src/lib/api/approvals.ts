/**
 * Approvals: the ONE place the UI asks "what needs a human decision?".
 *
 * This is the product's central question, and the backend had no single answer
 * for it. A policy `ASK` decision is recorded in FOUR unrelated places
 * depending on which subsystem asked:
 *
 *   A2A task delegation   -> /a2a/tasks        status pending_approval
 *   proactive workflows   -> /workflows        status waiting_approval
 *   autonomy runs         -> /autonomy/runs    status waiting_approval
 *   orchestration runs    -> /orchestration/runs state WAITING_APPROVAL
 *
 * Each has its own approve endpoint and its own field names for the same
 * concept. Rather than make the user visit four pages - or teach the UI four
 * vocabularies - this module fans out to all four in parallel and normalises
 * them into one list. A source that is unavailable (a subsystem that is not
 * configured, or orchestration being disabled by decision D5) is reported as
 * unavailable rather than failing the whole inbox, because one missing
 * subsystem must not hide the approvals that DO exist.
 */

import { apiFetch, ApiError } from "./client";

export type ApprovalSource = "task" | "workflow" | "autonomy" | "orchestration";

export type ApprovalItem = {
  /** Stable key: `${source}:${id}`. */
  id: string;
  source: ApprovalSource;
  /** The underlying record's id, for the approve call. */
  recordId: string;
  title: string;
  summary: string;
  /** What will happen if approved, in plain language. */
  requestedAction?: string | null;
  /** Who or what is asking. */
  requestedBy?: string | null;
  /** The policy dimensions, so the user can see what is being disclosed. */
  category?: string | null;
  purpose?: string | null;
  createdAt?: string | null;
  expiresAt?: string | null;
  riskLevel?: string | null;
};

export type ApprovalsResult = {
  items: ApprovalItem[];
  /** Sources that could not be read, with the reason. */
  unavailable: Array<{ source: ApprovalSource; reason: string }>;
};

const SOURCE_LABELS: Record<ApprovalSource, string> = {
  task: "Remote agent",
  workflow: "Workflow",
  autonomy: "Autonomy",
  orchestration: "Orchestration",
};

export function sourceLabel(source: ApprovalSource): string {
  return SOURCE_LABELS[source] ?? source;
}

/** Only these statuses mean "a human must decide". */
const PENDING_STATUSES = new Set([
  "pending_approval",
  "waiting_approval",
  "approval_required",
]);

function isPending(status: unknown): boolean {
  return typeof status === "string" && PENDING_STATUSES.has(status);
}

function short(text: unknown, max = 160): string {
  if (typeof text !== "string") return "";
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

type Fetcher = () => Promise<ApprovalItem[]>;

async function loadSource(
  source: ApprovalSource,
  fetchItems: Fetcher
): Promise<{ items: ApprovalItem[] } | { error: string }> {
  try {
    return { items: await fetchItems() };
  } catch (err) {
    // A missing subsystem is not an error the user should have to triage: a
    // 503 here just means that feature is not configured on this deployment.
    const reason =
      err instanceof ApiError
        ? err.status === 503
          ? "not configured on this deployment"
          : err.message
        : "unavailable";
    return { error: reason };
  }
}

async function loadTasks(): Promise<ApprovalItem[]> {
  const data = await apiFetch<{ tasks: any[] }>("/a2a/tasks");
  return (data.tasks ?? [])
    .filter((t) => isPending(t.status))
    .map((t) => ({
      id: `task:${t.task_id}`,
      source: "task" as const,
      recordId: t.task_id,
      title: `Task from ${t.sender_agent_id?.slice(0, 24) ?? "a remote agent"}`,
      summary: short(
        t.request_payload?.message ??
          t.request_payload?.query ??
          t.task_type ??
          "A remote agent is requesting an action."
      ),
      requestedAction: t.task_type ?? t.request_payload?.action ?? null,
      requestedBy: t.sender_agent_id ?? null,
      category: t.request_payload?.data_category ?? null,
      purpose: t.purpose ?? null,
      createdAt: t.created_at ?? null,
      expiresAt: t.expires_at ?? null,
    }));
}

async function loadWorkflows(): Promise<ApprovalItem[]> {
  const data = await apiFetch<{ workflows: any[] }>("/workflows");
  return (data.workflows ?? [])
    .filter((w) => isPending(w.status))
    .map((w) => ({
      id: `workflow:${w.workflow_id}`,
      source: "workflow" as const,
      recordId: w.workflow_id,
      title: w.workflow_type?.replace(/[_-]/g, " ") ?? "Workflow",
      summary: short(
        w.failure_reason ??
          `A workflow is paused at step ${w.current_step_number ?? "?"} and needs your decision.`
      ),
      requestedBy: "your agent",
      purpose: w.purpose ?? null,
      createdAt: w.created_at ?? null,
      expiresAt: w.expires_at ?? null,
    }));
}

async function loadAutonomy(): Promise<ApprovalItem[]> {
  const data = await apiFetch<{ runs?: any[] }>("/autonomy/runs");
  const runs = data.runs ?? (Array.isArray(data) ? (data as any[]) : []);
  return runs
    .filter((r) => isPending(r.status))
    .map((r) => ({
      id: `autonomy:${r.id ?? r.run_id}`,
      source: "autonomy" as const,
      recordId: String(r.id ?? r.run_id),
      title: r.goal ? short(r.goal, 80) : "Autonomous run",
      summary: short(
        r.approval_prompt ??
          r.stop_reason ??
          "An autonomous run wants to take an action."
      ),
      requestedAction: r.requested_action ?? null,
      requestedBy: "your agent",
      category: r.risk_level ?? null,
      purpose: r.mode ?? null,
      createdAt: r.created_at ?? null,
    }));
}

/** Only this orchestration state means "a human must decide".
 *
 * OrchestrationRunResponse carries `state`, not `status`, and its values are
 * the OrchestrationState enum (UPPERCASE: WAITING_APPROVAL, ...), unlike the
 * lowercase statuses of the other three sources. Compared case-insensitively
 * so either spelling surfaces instead of silently emptying the source. Every
 * other state (waiting on a remote peer, executing, terminal) correctly yields
 * no item: there is nothing for the owner to decide.
 */
function isOrchestrationPending(state: unknown): boolean {
  return (
    typeof state === "string" && state.toUpperCase() === "WAITING_APPROVAL"
  );
}

async function loadOrchestration(): Promise<ApprovalItem[]> {
  const data = await apiFetch<{ runs?: any[] }>("/orchestration/runs");
  const runs = data.runs ?? (Array.isArray(data) ? (data as any[]) : []);
  return runs
    .filter((r) => isOrchestrationPending(r.state))
    .map((r) => ({
      id: `orchestration:${r.run_id}`,
      source: "orchestration" as const,
      recordId: String(r.run_id),
      title: r.goal ? short(r.goal, 80) : "Orchestration run",
      summary: short(
        r.approval_prompt ??
          r.approval_reason ??
          "Your agent wants to contact someone on your behalf."
      ),
      requestedAction: r.requested_action ?? null,
      requestedBy: r.target_person ?? "your agent",
      category: r.approval_category ?? null,
      purpose: r.approval_purpose ?? null,
      createdAt: r.created_at ?? null,
    }));
}

export async function listApprovals(): Promise<ApprovalsResult> {
  const [task, workflow, autonomy, orchestration] = await Promise.all([
    loadSource("task", loadTasks),
    loadSource("workflow", loadWorkflows),
    loadSource("autonomy", loadAutonomy),
    loadSource("orchestration", loadOrchestration),
  ]);

  const items: ApprovalItem[] = [];
  const unavailable: ApprovalsResult["unavailable"] = [];

  for (const [source, outcome] of [
    ["task", task],
    ["workflow", workflow],
    ["autonomy", autonomy],
    ["orchestration", orchestration],
  ] as Array<[ApprovalSource, { items?: ApprovalItem[]; error?: string }]>) {
    if (outcome.items) items.push(...outcome.items);
    else if (outcome.error) unavailable.push({ source, reason: outcome.error });
  }

  // Oldest first: an approval that has been waiting longest deserves attention.
  items.sort((a, b) => (a.createdAt ?? "").localeCompare(b.createdAt ?? ""));
  return { items, unavailable };
}

const APPROVE_PATHS: Record<ApprovalSource, (id: string) => string> = {
  task: (id) => `/a2a/tasks/${id}/approve`,
  workflow: (id) => `/workflows/${id}/approve`,
  autonomy: (id) => `/autonomy/runs/${id}/approve`,
  orchestration: (id) => `/orchestration/runs/${id}/approve`,
};

const REJECT_PATHS: Record<ApprovalSource, (id: string) => string> = {
  task: (id) => `/a2a/tasks/${id}/reject`,
  workflow: (id) => `/workflows/${id}/cancel`,
  autonomy: (id) => `/autonomy/runs/${id}/reject`,
  orchestration: (id) => `/orchestration/runs/${id}/reject`,
};

export async function decideApproval(
  source: ApprovalSource,
  recordId: string,
  decision: "approve" | "deny"
): Promise<void> {
  const path =
    decision === "approve"
      ? APPROVE_PATHS[source](recordId)
      : REJECT_PATHS[source](recordId);
  await apiFetch(path, {
    method: "POST",
    // Each endpoint accepts an optional body with the owner's note; sending an
    // empty object keeps one call shape for all four.
    body: JSON.stringify(
      decision === "approve"
        ? { notes: "Approved" }
        : { reason: "Declined by owner" }
    ),
  });
}
