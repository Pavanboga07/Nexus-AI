"use client";

import React, { useEffect, useState } from "react";
import { PageShell } from "@/components/ui/PageShell";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { listTools, executeTool, getToolAudit } from "@/lib/api/tools";
import { ToolInfo } from "@/types/api";
import { formatDate, formatDuration } from "@/lib/utils";
import {
  Wrench,
  Play,
  RefreshCw,
  Activity,
} from "lucide-react";

export default function ToolsPage() {
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [auditEntries, setAuditEntries] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  // Tool execution modal
  const [selectedTool, setSelectedTool] = useState<ToolInfo | null>(null);
  const [paramInput, setParamInput] = useState("{}");
  const [executing, setExecuting] = useState(false);
  const [execResult, setExecResult] = useState<any>(null);

  const fetchTools = async () => {
    try {
      setLoading(true);
      const [toolRes, auditRes] = await Promise.allSettled([
        listTools(),
        getToolAudit(),
      ]);
      if (toolRes.status === "fulfilled") setTools(toolRes.value.tools || []);
      if (auditRes.status === "fulfilled")
        setAuditEntries(auditRes.value.entries || []);
    } catch (err) {
      console.error("Failed to load tools data", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTools();
  }, []);

  const handleOpenRunner = (tool: ToolInfo) => {
    setSelectedTool(tool);
    setExecResult(null);

    // Generate sample arguments based on parameters schema
    let initialParams: Record<string, any> = {};
    if (tool.name === "echo") {
      initialParams = { text: "Hello from Nexus command center" };
    } else if (tool.name === "get_current_time") {
      initialParams = {};
    } else if (tool.name === "calculator") {
      initialParams = { expression: "42 * 10 + 5" };
    } else if (tool.parameters?.properties) {
      Object.keys(tool.parameters.properties).forEach((k) => {
        initialParams[k] = "";
      });
    }
    setParamInput(JSON.stringify(initialParams, null, 2));
  };

  const handleExecute = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedTool) return;

    let parsedArgs = {};
    try {
      parsedArgs = JSON.parse(paramInput);
    } catch {
      alert("Parameters must be valid JSON.");
      return;
    }

    try {
      setExecuting(true);
      setExecResult(null);
      const res = await executeTool(selectedTool.name, parsedArgs);
      setExecResult(res);
      // Refresh audit logs
      const auditRes = await getToolAudit();
      setAuditEntries(auditRes.entries || []);
    } catch (err: any) {
      setExecResult({
        success: false,
        error: err.message || "Execution error",
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
          onClick={fetchTools}
        >
          <RefreshCw className="w-4 h-4 mr-2" />
          Refresh
        </Button>
      }
    >
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
          ) : tools.length === 0 ? (
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
                      {tool.requires_policy ? (
                        <Badge variant="warning">Policy Gated</Badge>
                      ) : (
                        <Badge variant="success">Unrestricted</Badge>
                      )}
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
                      {formatDate(entry.timestamp)} • Caller: {entry.caller} • Duration: {formatDuration(entry.duration_ms)}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {entry.success ? (
                      <Badge variant="success">SUCCESS</Badge>
                    ) : (
                      <Badge variant="danger">
                        FAILED
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
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs text-neutral-400 block">
                  Invocation Parameters (JSON)
                </label>
                {selectedTool.requires_policy && (
                  <span className="text-xs text-amber-500">
                    ⚠️ Policy authorization will be evaluated
                  </span>
                )}
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
