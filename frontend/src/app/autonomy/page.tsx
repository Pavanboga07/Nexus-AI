"use client";

import React, { useEffect, useState } from "react";
import { PageShell } from "@/components/ui/PageShell";
import { StatusBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import {
  getAutonomyConfig,
  updateAutonomyConfig,
  listAutonomyRuns,
  getAutonomyRun,
  createAutonomyRun,
  approveAutonomyRun,
  rejectAutonomyRun,
  cancelAutonomyRun,
  listAutonomyDecisions,
} from "@/lib/api/autonomy";
import {
  AutonomyConfigOut,
  AutonomyRunOut,
  AutonomyDecisionOut,
  AutonomyMode,
} from "@/types/api";
import { formatDate } from "@/lib/utils";
import {
  Plus,
  RefreshCw,
  Sliders,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  StopCircle,
} from "lucide-react";

export default function AutonomyPage() {
  const [config, setConfig] = useState<AutonomyConfigOut | null>(null);
  const [runs, setRuns] = useState<AutonomyRunOut[]>([]);
  const [selectedRun, setSelectedRun] = useState<AutonomyRunOut | null>(null);
  const [decisions, setDecisions] = useState<AutonomyDecisionOut[]>([]);
  const [statusFilter, setStatusFilter] = useState("all");
  const [loading, setLoading] = useState(false);
  const [updatingConfig, setUpdatingConfig] = useState(false);

  // New Goal Modal
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [goal, setGoal] = useState("");
  const [creating, setCreating] = useState(false);

  const fetchConfig = async () => {
    try {
      const cfg = await getAutonomyConfig();
      setConfig(cfg);
    } catch (err) {
      console.error("Failed to fetch autonomy config", err);
    }
  };

  const fetchRuns = async (status?: string) => {
    try {
      setLoading(true);
      const res = await listAutonomyRuns(status);
      setRuns(res.runs || []);
    } catch (err) {
      console.error("Failed to load autonomy runs", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchConfig();
  }, []);

  useEffect(() => {
    fetchRuns(statusFilter);
  }, [statusFilter]);

  const handleSelectRun = async (run: AutonomyRunOut) => {
    try {
      const fresh = await getAutonomyRun(run.id);
      setSelectedRun(fresh);
      const decRes = await listAutonomyDecisions(run.id);
      setDecisions(decRes.decisions || []);
    } catch (err) {
      setSelectedRun(run);
      setDecisions(run.decisions || []);
    }
  };

  const handleModeChange = async (newMode: AutonomyMode) => {
    try {
      setUpdatingConfig(true);
      const updated = await updateAutonomyConfig({ mode: newMode });
      setConfig(updated);
    } catch (err: any) {
      alert(`Failed to update mode: ${err.message}`);
    } finally {
      setUpdatingConfig(false);
    }
  };

  const handleToggleSetting = async (key: keyof AutonomyConfigOut, val: boolean) => {
    try {
      setUpdatingConfig(true);
      const updated = await updateAutonomyConfig({ [key]: val });
      setConfig(updated);
    } catch (err: any) {
      alert(`Failed to update setting: ${err.message}`);
    } finally {
      setUpdatingConfig(false);
    }
  };

  const handleCreateRun = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!goal.trim()) return;

    try {
      setCreating(true);
      const newRun = await createAutonomyRun({
        goal: goal.trim(),
        execute_immediately: true,
      });
      setGoal("");
      setCreateModalOpen(false);
      await fetchRuns(statusFilter);
      handleSelectRun(newRun);
    } catch (err: any) {
      alert(`Failed to trigger autonomous run: ${err.message}`);
    } finally {
      setCreating(false);
    }
  };

  const handleApprove = async (runId: string) => {
    const notes = prompt("Approval notes (optional):", "Approved by owner") ?? undefined;
    try {
      const res = await approveAutonomyRun(runId, notes);
      await fetchRuns(statusFilter);
      handleSelectRun(res);
    } catch (err: any) {
      alert(`Failed to approve run: ${err.message}`);
    }
  };

  const handleReject = async (runId: string) => {
    const notes = prompt("Rejection reason (optional):", "Rejected by owner") ?? undefined;
    try {
      const res = await rejectAutonomyRun(runId, notes);
      await fetchRuns(statusFilter);
      handleSelectRun(res);
    } catch (err: any) {
      alert(`Failed to reject run: ${err.message}`);
    }
  };

  const handleCancel = async (runId: string) => {
    if (!confirm("Are you sure you want to stop this autonomous run?")) return;
    try {
      const res = await cancelAutonomyRun(runId, "Stopped by owner");
      await fetchRuns(statusFilter);
      handleSelectRun(res);
    } catch (err: any) {
      alert(`Failed to stop run: ${err.message}`);
    }
  };

  const getDecisionBadge = (decision: string) => {
    switch (decision.toLowerCase()) {
      case "allow":
        return <span className="text-emerald-400 bg-emerald-950/40 border border-emerald-800/40 text-xs px-2 py-0.5 rounded font-mono">ALLOW</span>;
      case "ask":
        return <span className="text-amber-400 bg-amber-950/40 border border-amber-800/40 text-xs px-2 py-0.5 rounded font-mono">ASK (APPROVAL)</span>;
      case "deny":
        return <span className="text-red-400 bg-red-950/40 border border-red-800/40 text-xs px-2 py-0.5 rounded font-mono">DENY</span>;
      case "stop":
        return <span className="text-purple-400 bg-purple-950/40 border border-purple-800/40 text-xs px-2 py-0.5 rounded font-mono">STOP</span>;
      default:
        return <span className="text-neutral-400 font-mono text-xs">{decision}</span>;
    }
  };

  return (
    <PageShell
      title="Autonomy & Decision Engine"
      subtitle="Deterministic policy control, bounded goal execution, and human-in-the-loop approvals"
      action={
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              fetchConfig();
              fetchRuns(statusFilter);
            }}
            disabled={loading}
          >
            <RefreshCw className={`w-3.5 h-3.5 mr-1.5 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => setCreateModalOpen(true)}
          >
            <Plus className="w-3.5 h-3.5 mr-1.5" />
            New Goal
          </Button>
        </div>
      }
    >
      <div className="space-y-6">
        {/* Top Section: Autonomy Configuration Card */}
        <div className="bg-neutral-900/60 border border-neutral-800 rounded-lg p-5">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <Sliders className="w-5 h-5 text-neutral-300" />
              <h2 className="text-sm font-semibold text-neutral-200">Autonomy Operating Mode</h2>
            </div>
            <span className="text-xs text-neutral-500">Security Invariant: LLM is never the authority</span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mb-5">
            {[
              { id: "off", name: "Disabled (OFF)", desc: "Autonomous actions halted" },
              { id: "assisted", name: "Assisted", desc: "Owner must approve each action" },
              { id: "bounded", name: "Bounded (Safe)", desc: "Autonomous within strict budget & policy" },
              { id: "fully_delegated", name: "Fully Delegated", desc: "Policy bounds apply; minimal prompts" },
            ].map((m) => {
              const active = config?.mode === m.id;
              return (
                <button
                  key={m.id}
                  onClick={() => handleModeChange(m.id as AutonomyMode)}
                  disabled={updatingConfig}
                  className={`text-left p-3 rounded-lg border transition-all ${
                    active
                      ? "bg-neutral-800 border-neutral-600 text-neutral-100 shadow-sm"
                      : "bg-neutral-950/40 border-neutral-800/80 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300"
                  }`}
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-semibold">{m.name}</span>
                    {active && <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />}
                  </div>
                  <p className="text-[11px] text-neutral-500 leading-relaxed">{m.desc}</p>
                </button>
              );
            })}
          </div>

          {/* Hard safety toggles */}
          {config && (
            <div className="pt-4 border-t border-neutral-800/60 flex flex-wrap items-center justify-between gap-4 text-xs text-neutral-400">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={config.require_approval_for_external_communication}
                  onChange={(e) =>
                    handleToggleSetting("require_approval_for_external_communication", e.target.checked)
                  }
                  className="rounded border-neutral-700 bg-neutral-950 text-neutral-200 focus:ring-0"
                />
                Require approval for external agent communication
              </label>

              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={config.require_approval_for_sensitive_data}
                  onChange={(e) =>
                    handleToggleSetting("require_approval_for_sensitive_data", e.target.checked)
                  }
                  className="rounded border-neutral-700 bg-neutral-950 text-neutral-200 focus:ring-0"
                />
                Require approval for sensitive data
              </label>

              <div className="text-neutral-500">
                Budget: <span className="text-neutral-300 font-mono">{config.max_steps_per_run} steps max</span>
              </div>
            </div>
          )}
        </div>

        {/* Main Content: Split layout (Runs List vs Selected Run Inspector) */}
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
          {/* Runs Column */}
          <div className="lg:col-span-5 space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-semibold text-neutral-400 uppercase tracking-wider">
                Autonomous Runs ({runs.length})
              </h3>
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="text-xs bg-neutral-900 border border-neutral-800 rounded px-2 py-1 text-neutral-300"
              >
                <option value="all">All statuses</option>
                <option value="waiting_approval">Waiting Approval</option>
                <option value="running">Running</option>
                <option value="completed">Completed</option>
                <option value="stopped">Stopped</option>
                <option value="failed">Failed</option>
              </select>
            </div>

            <div className="space-y-2">
              {runs.length === 0 ? (
                <div className="p-8 text-center text-xs text-neutral-500 border border-dashed border-neutral-800 rounded-lg">
                  No autonomous runs found. Click "New Goal" to trigger an autonomous task.
                </div>
              ) : (
                runs.map((r) => {
                  const isSelected = selectedRun?.id === r.id;
                  const isPending = r.status === "waiting_approval";
                  return (
                    <div
                      key={r.id}
                      onClick={() => handleSelectRun(r)}
                      className={`p-3.5 rounded-lg border cursor-pointer transition-all ${
                        isSelected
                          ? "bg-neutral-850 border-neutral-600 shadow-sm"
                          : "bg-neutral-900/40 border-neutral-800/80 hover:bg-neutral-900/80 hover:border-neutral-700"
                      } ${isPending ? "ring-1 ring-amber-500/40" : ""}`}
                    >
                      <div className="flex items-start justify-between gap-2 mb-1.5">
                        <span className="text-xs font-medium text-neutral-200 line-clamp-1">
                          {r.goal}
                        </span>
                        <StatusBadge status={r.status} />
                      </div>
                      <div className="flex items-center justify-between text-[11px] text-neutral-500">
                        <span>
                          Step {r.current_step} / {r.plan?.length || r.steps_executed} executed
                        </span>
                        <span>{formatDate(r.created_at)}</span>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </div>

          {/* Inspector Column */}
          <div className="lg:col-span-7">
            {selectedRun ? (
              <div className="bg-neutral-900/60 border border-neutral-800 rounded-lg p-5 space-y-6">
                {/* Header */}
                <div className="flex items-start justify-between border-b border-neutral-800/60 pb-4">
                  <div>
                    <div className="flex items-center gap-2 mb-1">
                      <h3 className="text-sm font-semibold text-neutral-100">{selectedRun.goal}</h3>
                      <StatusBadge status={selectedRun.status} />
                    </div>
                    <div className="text-xs text-neutral-500 font-mono">
                      Run ID: {selectedRun.id}
                    </div>
                  </div>

                  <div className="flex items-center gap-2">
                    {selectedRun.status === "running" && (
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => handleCancel(selectedRun.id)}
                      >
                        <StopCircle className="w-3.5 h-3.5 mr-1" />
                        Stop
                      </Button>
                    )}
                  </div>
                </div>

                {/* Approval Needed Banner */}
                {selectedRun.status === "waiting_approval" && (
                  <div className="bg-amber-950/30 border border-amber-800/60 rounded-lg p-4 space-y-3">
                    <div className="flex items-center gap-2 text-amber-300 text-xs font-semibold">
                      <AlertTriangle className="w-4 h-4 text-amber-400" />
                      Approval Required to Proceed
                    </div>
                    {selectedRun.approvals && selectedRun.approvals.length > 0 && (
                      <div className="text-xs text-neutral-300 space-y-1 bg-neutral-950/60 p-3 rounded border border-amber-900/30">
                        <div>
                          <span className="text-neutral-500">Requested Action:</span>{" "}
                          <span className="font-semibold">{selectedRun.approvals[0].requested_action}</span>
                        </div>
                        <div>
                          <span className="text-neutral-500">Purpose:</span> {selectedRun.approvals[0].purpose}
                        </div>
                        <div>
                          <span className="text-neutral-500">Risk Level:</span>{" "}
                          <span className="uppercase text-amber-400">{selectedRun.approvals[0].risk_level}</span>
                        </div>
                      </div>
                    )}
                    <div className="flex items-center gap-2 pt-1">
                      <Button
                        variant="primary"
                        size="sm"
                        onClick={() => handleApprove(selectedRun.id)}
                      >
                        <CheckCircle2 className="w-3.5 h-3.5 mr-1.5" />
                        Approve & Resume
                      </Button>
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => handleReject(selectedRun.id)}
                      >
                        <XCircle className="w-3.5 h-3.5 mr-1.5" />
                        Reject & Abort
                      </Button>
                    </div>
                  </div>
                )}

                {/* Reasons if stopped or failed */}
                {selectedRun.failure_reason && (
                  <div className="bg-red-950/30 border border-red-800/40 rounded p-3 text-xs text-red-300">
                    <span className="font-semibold">Failure reason:</span> {selectedRun.failure_reason}
                  </div>
                )}
                {selectedRun.stop_reason && (
                  <div className="bg-purple-950/30 border border-purple-800/40 rounded p-3 text-xs text-purple-300">
                    <span className="font-semibold">Stop reason:</span> {selectedRun.stop_reason}
                  </div>
                )}

                {/* Execution Plan & Decisions */}
                <div className="space-y-3">
                  <h4 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider">
                    Deterministic Decisions & Audit ({decisions.length})
                  </h4>

                  <div className="space-y-2">
                    {decisions.length === 0 ? (
                      <p className="text-xs text-neutral-500">No decisions recorded for this run yet.</p>
                    ) : (
                      decisions.map((dec, i) => (
                        <div
                          key={dec.decision_id || i}
                          className="p-3 rounded border border-neutral-800 bg-neutral-950/50 space-y-1 text-xs"
                        >
                          <div className="flex items-center justify-between">
                            <span className="font-mono text-neutral-200">
                              [{dec.action_type}] {dec.proposed_action}
                            </span>
                            {getDecisionBadge(dec.decision)}
                          </div>
                          <div className="text-[11px] text-neutral-400">
                            Reason: {dec.reason}
                          </div>
                          <div className="flex items-center gap-3 text-[10px] text-neutral-500 pt-1">
                            <span>Risk: <span className="uppercase text-neutral-400">{dec.risk_level}</span></span>
                            <span>Trigger: {dec.trigger}</span>
                            {dec.policy_decision && (
                              <span>Policy Authority: {dec.policy_decision}</span>
                            )}
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                </div>
              </div>
            ) : (
              <div className="h-full min-h-[300px] flex items-center justify-center border border-dashed border-neutral-800 rounded-lg text-xs text-neutral-500">
                Select a run on the left to inspect its decisions, approval state, and audit records.
              </div>
            )}
          </div>
        </div>
      </div>

      {/* New Goal Modal */}
      <Modal
        isOpen={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        title="Trigger Autonomous Goal"
      >
        <form onSubmit={handleCreateRun} className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-neutral-300 mb-1">
              Goal Description
            </label>
            <textarea
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="e.g., Coordinate a lunch meeting with Sarah for Thursday"
              rows={3}
              required
              className="w-full text-xs bg-neutral-900 border border-neutral-800 rounded-md p-2.5 text-neutral-200 focus:outline-none focus:border-neutral-600"
            />
            <p className="text-[11px] text-neutral-500 mt-1">
              The bounded planner will produce a step-by-step allowlisted plan. Every step will be deterministically evaluated by the Decision Engine.
            </p>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => setCreateModalOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={creating || !goal.trim()}
            >
              {creating ? "Submitting..." : "Start Autonomous Run"}
            </Button>
          </div>
        </form>
      </Modal>
    </PageShell>
  );
}
