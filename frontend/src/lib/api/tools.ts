import { apiFetch } from "./client";
import { ToolInfo, ToolExecuteResponse } from "@/types/api";

export async function listTools(): Promise<{
  tools: ToolInfo[];
  total: number;
}> {
  return apiFetch<{ tools: ToolInfo[]; total: number }>("/tools");
}

export async function getTool(name: string): Promise<ToolInfo> {
  return apiFetch<ToolInfo>(`/tools/${name}`);
}

export async function executeTool(
  toolName: string,
  parameters: Record<string, unknown>,
  purpose: string = "testing"
): Promise<ToolExecuteResponse> {
  return apiFetch<ToolExecuteResponse>("/tools/execute", {
    method: "POST",
    body: JSON.stringify({
      tool_name: toolName,
      arguments: parameters,
      purpose,
    }),
  });
}

export type ToolAuditEntry = {
  id: string;
  request_id: string;
  tool_name: string;
  purpose: string;
  status: string;
  policy_decision: string;
  error_code?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
};

/**
 * The tool execution audit (`ToolAuditEntryOut`).
 *
 * Previously reshaped into `{entries: [{caller, duration_ms, success, ...}]}`.
 * Only `duration_ms: 0` and a `success` boolean derived from `status` survived
 * that mapping; `caller` was fed from `purpose`, so the Activity page labelled a
 * purpose as a caller, and every duration displayed as 0ms. The API shape is
 * used directly.
 */
export async function getToolAudit(): Promise<{
  executions: ToolAuditEntry[];
  total: number;
}> {
  return apiFetch<{ executions: ToolAuditEntry[]; total: number }>(
    "/tools/audit/list"
  );
}
