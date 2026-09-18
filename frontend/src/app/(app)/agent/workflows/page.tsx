"use client";

import React, { useEffect, useState } from "react";
import { PageShell } from "@/components/ui/PageShell";
import { StatusBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { ErrorState } from "@/components/ui/ErrorState";
import { useAsync } from "@/lib/useAsync";
import {
  listWorkflows,
  getWorkflow,
  createWorkflow,
  startWorkflow,
  approveWorkflow,
  cancelWorkflow,
} from "@/lib/api/workflows";
import { listTrustedAgents } from "@/lib/api/a2a";
import { WorkflowOut, TrustedAgent } from "@/types/api";
import { formatDate, truncateId } from "@/lib/utils";
import {
  Play,
  Plus,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";

export default function WorkflowsPage() {
  const [statusFilter, setStatusFilter] = useState("all");
  // A fetch failure renders an error with Retry instead of a false empty view.
  const {
    data: workflowsData,
    error: loadError,
    loading,
    reload: reloadWorkflows,
  } = useAsync(() =>
    listWorkflows(statusFilter === "all" ? undefined : statusFilter).then(
      (res) => res.workflows ?? []
    )
  );
  const workflows = workflowsData ?? [];
  useEffect(() => {
    reloadWorkflows();
  }, [statusFilter, reloadWorkflows]);

  const [trustedAgents, setTrustedAgents] = useState<TrustedAgent[]>([]);
  const [selectedWorkflow, setSelectedWorkflow] = useState<WorkflowOut | null>(null);
  /** Mutation failures render inline; never in a native dialog. */
  const [actionError, setActionError] = useState<string | null>(null);
  /** The row with a mutation in flight; its buttons show busy. */
  const [busyId, setBusyId] = useState<string | null>(null);
  const [cancelTarget, setCancelTarget] = useState<WorkflowOut | null>(null);

  // Create workflow modal
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [workflowType, setWorkflowType] = useState("meeting_coordination");
  const [purpose, setPurpose] = useState("Coordinate calendar sync with peer");
  const [selectedPeerId, setSelectedPeerId] = useState("");
  const [creating, setCreating] = useState(false);
  /** Form validation/creation errors render inside the modal. */
  const [createError, setCreateError] = useState<string | null>(null);

  const refreshWorkflows = () => {
    setActionError(null);
    reloadWorkflows();
  };

  useEffect(() => {
    listTrustedAgents()
      .then((res) => setTrustedAgents(res.agents || []))
      .catch(() => {});
  }, []);

  const handleStartWorkflow = async (id: string) => {
    if (busyId !== null) return;
    setBusyId(id);
    setActionError(null);
    try {
      await startWorkflow(id);
      refreshWorkflows();
      if (selectedWorkflow?.workflow_id === id) {
        const updated = await getWorkflow(id);
        setSelectedWorkflow(updated);
      }
    } catch (err: any) {
      setActionError(`Failed to start workflow: ${err.message}`);
    } finally {
      setBusyId(null);
    }
  };

  const handleApproveStep = async (id: string, stepId?: string) => {
    if (busyId !== null) return;
    setBusyId(id);
    setActionError(null);
    try {
      await approveWorkflow(id, stepId);
      refreshWorkflows();
      const updated = await getWorkflow(id);
      setSelectedWorkflow(updated);
    } catch (err: any) {
      setActionError(`Failed to approve workflow step: ${err.message}`);
    } finally {
      setBusyId(null);
    }
  };

  const handleCancelConfirm = async () => {
    if (!cancelTarget) return;
    if (busyId !== null) return;
    const id = cancelTarget.workflow_id;
    setCancelTarget(null);
    setBusyId(id);
    setActionError(null);
    try {
      await cancelWorkflow(id);
      refreshWorkflows();
      if (selectedWorkflow?.workflow_id === id) {
        const updated = await getWorkflow(id);
        setSelectedWorkflow(updated);
      }
    } catch (err: any) {
      setActionError(`Failed to cancel workflow: ${err.message}`);
    } finally {
      setBusyId(null);
    }
  };

  const handleCreateMeetingWorkflow = async (e: React.FormEvent) => {
    e.preventDefault();
    if (creating) return;
    if (!selectedPeerId) {
      setCreateError("Please select a trusted peer agent.");
      return;
    }

    try {
      setCreating(true);
      setCreateError(null);
      const created = await createWorkflow({
        workflow_type: "meeting_coordination",
        purpose: purpose || "Coordinate meeting with peer agent",
        steps: [
          {
            step_type: "availability_step",
            input_payload: { date: "2026-09-15", window: "afternoon" },
          },
          {
            step_type: "a2a_task_step",
            input_payload: {
              recipient_agent_id: selectedPeerId,
              task_type: "availability_query",
              query_date: "2026-09-15",
            },
          },
          {
            step_type: "candidate_selection",
            input_payload: { strategy: "earliest_overlap" },
          },
        ],
        context_data: { peer_agent_id: selectedPeerId },
      });

      setCreateModalOpen(false);
      refreshWorkflows();
      setSelectedWorkflow(created);
    } catch (err: any) {
      setCreateError(`Failed to create workflow: ${err.message}`);
    } finally {
      setCreating(false);
    }
  };

  const filters = [
    { id: "all", label: "All" },
    { id: "running", label: "Running" },
    { id: "waiting_approval", label: "Waiting Approval" },
    { id: "completed", label: "Completed" },
    { id: "failed", label: "Failed" },
  ];

  return (
    <PageShell 
      title="Workflows" 
      subtitle="Multi-step, durable state machine with independent step authorization & crash recovery"
      action={
        <Button
          size="sm"
          leftIcon={<Plus className="w-3.5 h-3.5" />}
          onClick={() => {
            setCreateError(null);
            setCreateModalOpen(true);
          }}
        >
          New Workflow
        </Button>
      }
    >
      <div className="flex items-center justify-between mb-6">
        <div className="flex gap-2">
          {filters.map(f => (
            <button
              key={f.id}
              onClick={() => setStatusFilter(f.id)}
              className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
                statusFilter === f.id 
                  ? "bg-neutral-800 text-neutral-100" 
                  : "text-neutral-400 hover:text-neutral-200"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <Button
          variant="outline"
          size="sm"
          leftIcon={<RefreshCw className="w-3.5 h-3.5" />}
          onClick={refreshWorkflows}
          disabled={loading}
        >
          Refresh
        </Button>
      </div>

      {loadError && <ErrorState message={loadError} onRetry={refreshWorkflows} />}
      {actionError && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-red-900/50 bg-red-950/40 px-3 py-2 text-xs text-red-300"
        >
          {actionError}
        </div>
      )}

      {loading ? (
        <div className="py-16 text-center">
          <p className="text-sm text-neutral-400">Loading workflows...</p>
        </div>
      ) : loadError && workflows.length === 0 ? null : workflows.length === 0 ? (
        <div className="py-16 text-center">
          <p className="text-sm text-neutral-400">No workflows found in this view.</p>
          <div className="mt-4">
            <Button
              size="sm"
              variant="outline"
              leftIcon={<Plus className="w-3.5 h-3.5" />}
              onClick={() => {
                setCreateError(null);
                setCreateModalOpen(true);
              }}
            >
              Create Meeting Coordination Flow
            </Button>
          </div>
        </div>
      ) : (
        <div className="divide-y divide-neutral-800">
          {workflows.map((wf) => (
            <div key={wf.workflow_id} className="py-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
              <div className="space-y-2 flex-1">
                <div className="flex items-center gap-2">
                  <StatusBadge status={wf.status} />
                  <span className="text-sm font-medium text-neutral-100">
                    {wf.workflow_type}
                  </span>
                  <span className="text-xs text-neutral-400">
                    — {wf.purpose}
                  </span>
                </div>

                <div className="flex flex-wrap items-center gap-4 pt-1">
                  {wf.steps?.map((step, idx) => {
                    const isDone = step.status === "completed";
                    const isWait = step.status === "waiting";
                    const isRunning = step.status === "running";
                    const isFail = step.status === "failed";
                    const icon = isDone ? "✓" : isRunning ? "●" : isFail ? "✕" : "○";
                    
                    return (
                      <div key={step.step_id} className="flex items-center gap-2">
                        <div className="flex items-center gap-1.5 text-xs text-neutral-400">
                          <span className={`${
                            isDone ? "text-emerald-500" :
                            isRunning ? "text-blue-500 animate-pulse" :
                            isWait ? "text-amber-500 animate-pulse" :
                            isFail ? "text-rose-500" :
                            "text-neutral-500"
                          }`}>
                            {icon}
                          </span>
                          <span>{step.step_type}</span>
                        </div>
                        {idx < wf.steps.length - 1 && (
                          <span className="text-neutral-700 text-xs">─</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>

              <div className="flex items-center gap-2 shrink-0">
                {wf.status === "pending" && (
                  <Button
                    size="sm"
                    variant="primary"
                    leftIcon={<Play className="w-3.5 h-3.5" />}
                    onClick={() => handleStartWorkflow(wf.workflow_id)}
                    isLoading={busyId === wf.workflow_id}
                    disabled={busyId !== null && busyId !== wf.workflow_id}
                  >
                    Start
                  </Button>
                )}
                {wf.status === "waiting_approval" && (
                  <Button
                    size="sm"
                    className="bg-amber-500 hover:bg-amber-400 text-neutral-950"
                    leftIcon={<ShieldAlert className="w-3.5 h-3.5" />}
                    onClick={() => handleApproveStep(wf.workflow_id)}
                    isLoading={busyId === wf.workflow_id}
                    disabled={busyId !== null && busyId !== wf.workflow_id}
                  >
                    Approve Step
                  </Button>
                )}
                {["running", "waiting_approval", "waiting_remote"].includes(
                  wf.status
                ) && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setCancelTarget(wf)}
                    disabled={busyId !== null}
                  >
                    Cancel
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setSelectedWorkflow(wf)}
                >
                  Details
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* New Workflow Modal */}
      <Modal
        isOpen={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        title="Create New Workflow"
        subtitle="Orchestrate multi-step tasks across tools and remote agents"
      >
        <form onSubmit={handleCreateMeetingWorkflow} className="space-y-4">
          {createError && (
            <p role="alert" className="text-xs text-red-400">
              {createError}
            </p>
          )}
          <div>
            <label className="text-xs text-neutral-400 font-medium block mb-1">
              Workflow Template
            </label>
            <select
              value={workflowType}
              onChange={(e) => setWorkflowType(e.target.value)}
              className="w-full bg-neutral-900 border border-neutral-800 rounded-md px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
            >
              <option value="meeting_coordination">
                Meeting Coordination (Privacy Preserving)
              </option>
            </select>
          </div>

          <div>
            <label className="text-xs text-neutral-400 font-medium block mb-1">
              Trusted Peer Agent
            </label>
            {trustedAgents.length === 0 ? (
              <div className="p-3 bg-amber-950/20 text-xs text-amber-500">
                No trusted agents available.
              </div>
            ) : (
              <select
                value={selectedPeerId}
                onChange={(e) => setSelectedPeerId(e.target.value)}
                className="w-full bg-neutral-900 border border-neutral-800 rounded-md px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500 font-mono"
              >
                <option value="">-- Select Peer Agent --</option>
                {trustedAgents.map((agt) => (
                  <option key={agt.agent_id} value={agt.agent_id}>
                    {agt.display_name} ({truncateId(agt.agent_id, 8)})
                  </option>
                ))}
              </select>
            )}
          </div>

          <div>
            <label className="text-xs text-neutral-400 font-medium block mb-1">
              Workflow Purpose
            </label>
            <input
              type="text"
              value={purpose}
              onChange={(e) => setPurpose(e.target.value)}
              placeholder="e.g. Coordinate meeting schedule"
              className="w-full bg-neutral-900 border border-neutral-800 rounded-md px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
            />
          </div>

          <div className="p-3 bg-neutral-900 border border-neutral-800 text-xs text-neutral-400 space-y-1">
            <span className="text-neutral-300 font-medium block">
              Automated Step Execution Plan:
            </span>
            <p>1. Local Availability Check</p>
            <p>2. A2A Candidate Window Query</p>
            <p>3. Candidate Intersection</p>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setCreateModalOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={creating || !selectedPeerId}
              isLoading={creating}
            >
              Create Workflow
            </Button>
          </div>
        </form>
      </Modal>

      {/* Workflow Inspection Modal */}
      {selectedWorkflow && (
        <Modal
          isOpen={!!selectedWorkflow}
          onClose={() => setSelectedWorkflow(null)}
          title={`Workflow: ${selectedWorkflow.workflow_type}`}
          subtitle={`ID: ${selectedWorkflow.workflow_id}`}
          maxWidth="2xl"
        >
          <div className="space-y-4">
            <div className="flex items-center justify-between pb-3 border-b border-neutral-800">
              <div className="flex items-center gap-2">
                <StatusBadge status={selectedWorkflow.status} />
                <span className="text-xs text-neutral-300">
                  {selectedWorkflow.purpose}
                </span>
              </div>
              <span className="text-xs text-neutral-400 font-mono">
                Expires: {formatDate(selectedWorkflow.expires_at)}
              </span>
            </div>

            {selectedWorkflow.status === "waiting_approval" && (
              <div className="p-4 bg-amber-950/20 border border-amber-900 flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <ShieldAlert className="w-5 h-5 text-amber-500 shrink-0" />
                  <div>
                    <h5 className="text-xs font-medium text-amber-500">
                      Step Requires Owner Consent
                    </h5>
                    <p className="text-xs text-amber-500/80">
                      Policy decision evaluated to ASK. Explicit permission is required.
                    </p>
                  </div>
                </div>
                <Button
                  size="sm"
                  className="bg-amber-500 hover:bg-amber-400 text-neutral-950 shrink-0"
                  onClick={() => handleApproveStep(selectedWorkflow.workflow_id)}
                  isLoading={busyId === selectedWorkflow.workflow_id}
                  disabled={busyId !== null && busyId !== selectedWorkflow.workflow_id}
                >
                  Approve Step
                </Button>
              </div>
            )}

            <div className="space-y-3">
              <label className="text-xs text-neutral-400 font-medium block">
                Workflow Step Sequence
              </label>
              {selectedWorkflow.steps?.map((step) => (
                <div
                  key={step.step_id}
                  className="p-3 bg-neutral-900 border border-neutral-800 space-y-2 text-xs"
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-neutral-400">
                        #{step.step_number}
                      </span>
                      <span className="font-medium text-neutral-200">
                        {step.step_type}
                      </span>
                      <StatusBadge status={step.status} />
                    </div>
                    <span className="text-neutral-500 font-mono text-xs">
                      Attempts: {step.attempt_count}/{step.max_attempts}
                    </span>
                  </div>

                  {step.input_payload != null && (
                    <div>
                      <span className="text-xs text-neutral-500">Input:</span>
                      <pre className="mt-1 p-2 bg-neutral-950 text-neutral-300 font-mono overflow-x-auto">
                        {JSON.stringify(step.input_payload, null, 2)}
                      </pre>
                    </div>
                  )}

                  {step.output_payload != null && (
                    <div>
                      <span className="text-xs text-emerald-500">Output:</span>
                      <pre className="mt-1 p-2 bg-neutral-950 text-neutral-300 font-mono overflow-x-auto">
                        {JSON.stringify(step.output_payload, null, 2)}
                      </pre>
                    </div>
                  )}

                  {step.failure_reason && (
                    <div className="text-rose-500">
                      Failure: {step.failure_reason}
                    </div>
                  )}
                </div>
              ))}
            </div>

            {selectedWorkflow.context_data != null && (
              <div>
                <label className="text-xs text-neutral-400 font-medium block mb-1">
                  Accumulated Context State
                </label>
                <pre className="p-3 bg-neutral-900 border border-neutral-800 text-xs text-neutral-300 font-mono overflow-x-auto">
                  {JSON.stringify(selectedWorkflow.context_data, null, 2)}
                </pre>
              </div>
            )}

            <div className="pt-3 border-t border-neutral-800 flex justify-end gap-2">
              {selectedWorkflow.status === "pending" && (
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => handleStartWorkflow(selectedWorkflow.workflow_id)}
                  isLoading={busyId === selectedWorkflow.workflow_id}
                  disabled={busyId !== null && busyId !== selectedWorkflow.workflow_id}
                >
                  Start Execution
                </Button>
              )}
              <Button
                size="sm"
                variant="outline"
                onClick={() => setSelectedWorkflow(null)}
              >
                Close
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* Cancel confirmation via modal, no native dialogs. */}
      <ConfirmModal
        open={cancelTarget !== null}
        title="Cancel workflow"
        body={`Stop "${cancelTarget?.workflow_type ?? "this workflow"}"? Steps that already ran are kept; nothing further executes.`}
        confirmLabel="Cancel workflow"
        danger
        onConfirm={() => void handleCancelConfirm()}
        onCancel={() => setCancelTarget(null)}
      />
    </PageShell>
  );
}
