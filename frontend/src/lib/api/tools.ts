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

export async function getToolAudit(): Promise<{
  entries: Array<{
    id: string;
    tool_name: string;
    caller: string;
    success: boolean;
    duration_ms: number;
    error?: string;
    timestamp: string;
  }>;
  total: number;
}> {
  const res = await apiFetch<{ executions: any[]; total: number }>(
    "/tools/audit/list"
  );
  return {
    entries: (res.executions || []).map((e) => ({
      id: e.id,
      tool_name: e.tool_name,
      caller: e.purpose,
      success: e.status === "executed",
      duration_ms: 0,
      error: e.error_code,
      timestamp: e.created_at,
    })),
    total: res.total,
  };
}
