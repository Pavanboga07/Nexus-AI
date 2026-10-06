"use client";

import React, { useState } from "react";
import { PageShell } from "@/components/ui/PageShell";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ErrorState } from "@/components/ui/ErrorState";
import { useAsync } from "@/lib/useAsync";
import {
  listTools,
  executeTool,
  getToolAudit,
  ToolAuditEntry,
} from "@/lib/api/tools";
import { ToolInfo, ToolExecuteResponse } from "@/types/api";
import { formatDate } from "@/lib/utils";
import {
  Wrench,
  Play,
  RefreshCw,
  Activity,
} from "lucide-react";
import { errorMessage } from "@/lib/api/client";

export default function ToolsPage() {
  // The two sources load together; a total failure throws so the page shows
  // an error with Retry, while a partial failure lists what is missing.
  const { data, error: loadError, loading, reload } = useAsync(async () => {
    const [toolRes, auditRes] = await Promise.allSettled([
      listTools(),
      getToolAudit(),
    ]);
    const failed: string[] = [];
    let tools: ToolInfo[] = [];
    let auditEntries: ToolAuditEntry[] = [];
    if (toolRes.status === "fulfilled") tools = toolRes.value.tools || [];
    else failed.push("registered tools");
    if (auditRes.status === "fulfilled")
      auditEntries = auditRes.value.executions || [];
    else failed.push("execution audit");
    if (failed.length === 2) {
      throw new Error(
        "Could not load tools. The tool subsystem may be unavailable."
      );
    }
    return { tools, auditEntries, failed };
  });
  const tools = data?.tools ?? [];
  const auditEntries = data?.auditEntries ?? [];
  const failedSources = data?.failed ?? [];

  const refreshTools = () => {
    setExecResult(null);
    reload();
  };

  // Tool execution modal
  const [selectedTool, setSelectedTool] = useState<ToolInfo | null>(null);
  const [paramInput, setParamInput] = useState("{}");
  const [executing, setExecuting] = useState(false);
  const [execResult, setExecResult] = useState<
    ToolExecuteResponse | { success: boolean; error: string } | null
  >(null);
  /** JSON/validation failures render inside the modal, not in a native dialog. */
  const [execError, setExecError] = useState<string | null>(null);

  const handleOpenRunner = (tool: ToolInfo) => {
    setSelectedTool(tool);
    setExecResult(null);
    setExecError(null);

    // Generate sample arguments based on parameters schema
    let initialParams: Record<string, any> = {};
    if (tool.name === "echo") {
      initialParams = { text: "Hello from Nexus command center" };
    } else if (tool.name === "get_current_time") {
      initialParams = {};
    } else if (tool.name === "calculator") {
      initialParams = { expression: "42 * 10 + 5" };
    } else if (tool.inputSchema?.properties) {
      // `inputSchema` is the real field name. This used to read
      // `tool.parameters.properties`, which is not in the response, so every
      // tool opened with a bare `{}` and no hint of its arguments.
      Object.keys(tool.inputSchema.properties).forEach((k) => {
        initialParams[k] = "";
      });
    }
    setParamInput(JSON.stringify(initialParams, null, 2));
  };

  const handleExecute = async (e: React.FormEvent) => {
    e.preventDefault();
    if (executing) return;
    if (!selectedTool) return;

    let parsedArgs = {};
    try {
      parsedArgs = JSON.parse(paramInput);
    } catch {
      setExecError("Parameters must be valid JSON.");
      return;
    }

    try {
      setExecuting(true);
      setExecResult(null);
      setExecError(null);
      const res = await executeTool(selectedTool.name, parsedArgs);
      setExecResult(res);
      // Refresh the audit trail behind the modal.
      reload();
    } catch (err) {
      setExecResult({
        success: false,
        error: errorMessage(err) || "Execution error",
      });
    } finally {
      setExecuting(false);
    }
  };

  return (
    <PageShell
      title="Tools"
      subtitle="Policy-gated Model Context Protocol tool execution & audit traces"
      action={
        <Button
          variant="outline"
          size="sm"
          onClick={refreshTools}
          disabled={loading}
        >
          <RefreshCw className="w-4 h-4 mr-2" />
          Refresh
        </Button>
      }
    >
      {loadError && <ErrorState message={loadError} onRetry={refreshTools} />}
      {failedSources.length > 0 && (
        <div className="mb-6 rounded-md border border-amber-900/50 bg-amber-950/30 px-3 py-2 text-xs text-amber-300">
          Some sources could not be read, so this view is incomplete:{" "}
          {failedSources.join(", ")}.
        </div>
      )}
      <div className="space-y-12">
        {/* Tools Section */}
        <div>
          <div className="mb-4">
            <h2 className="text-sm font-medium text-neutral-100 flex items-center gap-2">
              <Wrench className="w-4 h-4 text-neutral-400" />
              Registered Tools
            </h2>
          </div>
          
          {loading ? (
            <div className="py-16 text-center">
              <p className="text-sm text-neutral-400">Loading...</p>
            </div>
          ) : loadError && tools.length === 0 ? null : tools.length === 0 ? (
            <div className="py-16 text-center border border-neutral-800 rounded-lg">
              <p className="text-sm text-neutral-400">No tools registered in current process.</p>
            </div>
          ) : (
            <div className="divide-y divide-neutral-800 border-y border-neutral-800">
              {tools.map((tool) => (
                <div key={tool.name} className="py-4 flex items-center justify-between">
                  <div>
                    <div className="flex items-center gap-2">
                      <div className="text-sm font-medium text-neutral-100">{tool.name}</div>
                      {/* No "policy gated / unrestricted" badge: the tool list
                          does not report whether a tool requires policy - every
                          execution is evaluated by the engine - so the old badge
                          was asserting something the backend never said. */}
                    </div>
                    <div className="text-xs text-neutral-400 mt-1">
                      {tool.description || "No description provided."}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 pl-4">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => handleOpenRunner(tool)}
                      disabled={executing}
                    >
                      <Play className="w-4 h-4 mr-2" />
                      Execute
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Audit Section */}
        <div>
          <div className="mb-4">
            <h2 className="text-sm font-medium text-neutral-100 flex items-center gap-2">
              <Activity className="w-4 h-4 text-neutral-400" />
              Execution Audit
            </h2>
          </div>

          {auditEntries.length === 0 ? (
            <div className="py-16 text-center border border-neutral-800 rounded-lg">
              <p className="text-sm text-neutral-400">No tool executions recorded yet.</p>
            </div>
          ) : (
            <div className="divide-y divide-neutral-800 border-y border-neutral-800">
              {auditEntries.map((entry) => (
                <div key={entry.id} className="py-4 flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium text-neutral-100">
                      {entry.tool_name}
                    </div>
                    <div className="text-xs text-neutral-400 mt-0.5">
                      {formatDate(entry.created_at)} · Purpose: {entry.purpose} ·
                      Policy: {entry.policy_decision}
                      {entry.error_code ? ` · ${entry.error_code}` : ""}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {/* `status` is the field the backend records; the old code
                        read a `success` boolean that does not exist, so every
                        execution displayed as FAILED. */}
                    {entry.status === "executed" ? (
                      <Badge variant="success">SUCCESS</Badge>
                    ) : (
                      <Badge variant="danger">
                        {(entry.status || "failed").toUpperCase()}
                      </Badge>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Tool Execution Runner Modal */}
      {selectedTool && (
        <Modal
          isOpen={!!selectedTool}
          onClose={() => setSelectedTool(null)}
          title={`Execute Tool: ${selectedTool.name}`}
          subtitle="Directly test MCP tool invocation against policy authorization"
        >
          <form onSubmit={handleExecute} className="space-y-4">
            {execError && (
              <p role="alert" className="text-xs text-red-400">
                {execError}
              </p>
            )}
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs text-neutral-400 block">
                  Invocation Parameters (JSON)
                </label>
                {/* Stated unconditionally, because it is unconditionally true:
                    every tool call goes through the policy engine. */}
                <span className="text-xs text-amber-500">
                  Policy authorization is evaluated on every call
                </span>
              </div>
              <textarea
                value={paramInput}
                onChange={(e) => setParamInput(e.target.value)}
                rows={5}
                className="w-full bg-neutral-950 border border-neutral-800 rounded px-3 py-2 text-sm text-neutral-100 font-mono focus:outline-none focus:border-blue-500"
              />
            </div>

            <div className="pt-2 flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => setSelectedTool(null)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={executing}
                isLoading={executing}
              >
                Run Tool
              </Button>
            </div>
          </form>

          {/* Execution Result Box */}
          {execResult && (
            <div className="mt-6 pt-6 border-t border-neutral-800">
              <label className="text-xs text-neutral-400 block mb-2">
                Execution Output
              </label>
              <div
                className={`p-3 rounded border text-xs font-mono overflow-x-auto max-h-48 ${
                  execResult.success
                    ? "bg-neutral-950 border-emerald-500/30 text-emerald-500"
                    : "bg-neutral-950 border-rose-500/30 text-rose-500"
                }`}
              >
                <pre>{JSON.stringify(execResult, null, 2)}</pre>
              </div>
            </div>
          )}
        </Modal>
      )}
    </PageShell>
  );
}
