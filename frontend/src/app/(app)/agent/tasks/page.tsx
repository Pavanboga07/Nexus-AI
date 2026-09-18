"use client";

import React, { useEffect, useRef, useState } from "react";
import { PageShell } from "@/components/ui/PageShell";
import { Badge, StatusBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { ErrorState } from "@/components/ui/ErrorState";
import { useAsync } from "@/lib/useAsync";
import { useCapabilities } from "@/lib/useCapabilities";
import {
  listTasks,
  getTask,
  delegateTask,
  approveTask,
  rejectTask,
  cancelTask,
  negotiateTask,
  CAPABILITY_TASK_TYPES,
  payloadTemplateForTaskType,
} from "@/lib/api/tasks";
import { listTrustedAgents, getA2AAudit, A2AAuditEntry } from "@/lib/api/a2a";
import { A2ATask, TrustedAgent } from "@/types/api";
import { formatDate, truncateId } from "@/lib/utils";
import { useContactNames } from "@/lib/useContactNames";
import {
  Plus,
  RefreshCw,
} from "lucide-react";

export default function TasksPage() {
  const [statusFilter, setStatusFilter] = useState("all");
  // The list loads through useAsync so a fetch failure renders an error with
  // Retry instead of a false "No tasks found".
  const {
    data: tasksData,
    error: loadError,
    loading,
    reload: reloadTasks,
  } = useAsync(() =>
    listTasks(statusFilter === "all" ? undefined : statusFilter).then(
      (res) => res.tasks ?? []
    )
  );
  const tasks = tasksData ?? [];
  // The fetcher reads the filter through a ref, so a filter change needs an
  // explicit reload (this also fires once on mount — a harmless duplicate GET).
  useEffect(() => {
    reloadTasks();
  }, [statusFilter, reloadTasks]);

  const [trustedAgents, setTrustedAgents] = useState<TrustedAgent[]>([]);
  const [selectedTask, setSelectedTask] = useState<A2ATask | null>(null);
  /** Signed messages exchanged for the selected task, filtered from the audit. */
  const [timeline, setTimeline] = useState<A2AAuditEntry[]>([]);
  /** Mutation failures render inline; they never use a native dialog. */
  const [actionError, setActionError] = useState<string | null>(null);
  /** The row (or modal) with a mutation in flight; its buttons show busy. */
  const [busyId, setBusyId] = useState<string | null>(null);
  /** Pending destructive actions, confirmed through ConfirmModal. */
  const [rejectTarget, setRejectTarget] = useState<A2ATask | null>(null);
  const [cancelTarget, setCancelTarget] = useState<A2ATask | null>(null);

  // New task modal
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [recipientAgentId, setRecipientAgentId] = useState("");
  // The capability is picked from the live registry; the task_type the
  // delegate endpoint accepts is derived from it (see CAPABILITY_TASK_TYPES).
  const [capabilityId, setCapabilityId] = useState("");
  const [purpose, setPurpose] = useState("");
  const [payloadText, setPayloadText] = useState("");
  const [creating, setCreating] = useState(false);
  /** Form validation errors render inside the modal, not in a native dialog. */
  const [createError, setCreateError] = useState<string | null>(null);
  const { capabilities, loading: capsLoading, error: capsError, reload: reloadCaps } =
    useCapabilities();
  const taskType = CAPABILITY_TASK_TYPES[capabilityId] ?? "";
  // The last auto-filled payload, so picking another capability only
  // overwrites JSON the user has not edited themselves.
  const lastTemplate = useRef<string | null>(null);

  function pickCapability(id: string) {
    setCapabilityId(id);
    const mapped = CAPABILITY_TASK_TYPES[id];
    if (mapped && (payloadText.trim() === "" || payloadText === lastTemplate.current)) {
      const template = payloadTemplateForTaskType(mapped);
      lastTemplate.current = template;
      setPayloadText(template);
    }
  }

  // Preselect the first registry entry so the modal opens ready to send;
  // the user can still pick explicitly before dispatching.
  useEffect(() => {
    if (createModalOpen && capabilityId === "" && capabilities.length > 0) {
      pickCapability(capabilities[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [createModalOpen, capabilities]);

  // Negotiation modal
  const [negotiateModalOpen, setNegotiateModalOpen] = useState(false);
  const [counterTerms, setCounterTerms] = useState('{"candidate_time": "15:00"}');
  const [negotiating, setNegotiating] = useState(false);
  const [negotiateError, setNegotiateError] = useState<string | null>(null);
  // Peer IDs resolve to contact display names; unknown IDs fall back to a
  // short slice.
  const { resolve: resolveName } = useContactNames();

  const refreshTasks = () => {
    setActionError(null);
    reloadTasks();
  };

  useEffect(() => {
    listTrustedAgents()
      .then((res) => setTrustedAgents(res.agents || []))
      .catch(() => {});
  }, []);

  /**
   * Open a task, and load the signed messages behind it.
   *
   * There is no per-task message endpoint, so the audit is fetched and filtered
   * by `task_id` here. That keeps the timeline real: it shows the envelopes that
   * were actually signed and recorded, rather than a reconstruction.
   */
  const openTask = async (task: A2ATask) => {
    setSelectedTask(task);
    setTimeline([]);
    try {
      const { messages } = await getA2AAudit(200);
      setTimeline(messages.filter((m) => m.task_id === task.task_id));
    } catch {
      // The task itself is already shown; a missing timeline is a gap, not a
      // failed page, so it is left empty rather than replacing the whole view.
    }
  };

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (creating) return;
    if (!recipientAgentId) {
      setCreateError("Please select a recipient agent.");
      return;
    }
    if (!taskType) {
      setCreateError(
        capabilityId
          ? "This capability cannot be sent as a task yet."
          : "Please select a capability."
      );
      return;
    }
    let payloadObj = {};
    try {
      payloadObj = JSON.parse(payloadText || "{}");
    } catch {
      setCreateError("Payload must be valid JSON.");
      return;
    }

    try {
      setCreating(true);
      setCreateError(null);
      await delegateTask({
        recipient_agent_id: recipientAgentId,
        task_type: taskType,
        purpose: purpose || `Delegated ${taskType}`,
        payload: payloadObj,
      });
      setCreateModalOpen(false);
      setPurpose("");
      if (taskType) {
        const template = payloadTemplateForTaskType(taskType);
        lastTemplate.current = template;
        setPayloadText(template);
      }
      refreshTasks();
    } catch (err: any) {
      setCreateError(`Failed to delegate task: ${err.message}`);
    } finally {
      setCreating(false);
    }
  };

  const handleApprove = async (taskId: string) => {
    if (busyId !== null) return;
    setBusyId(taskId);
    setActionError(null);
    try {
      await approveTask(taskId);
      refreshTasks();
      if (selectedTask?.task_id === taskId) {
        const updated = await getTask(taskId);
        await openTask(updated);
      }
    } catch (err: any) {
      setActionError(`Failed to approve task: ${err.message}`);
    } finally {
      setBusyId(null);
    }
  };

  const handleRejectConfirm = async (reason?: string) => {
    if (!rejectTarget) return;
    if (busyId !== null) return;
    const taskId = rejectTarget.task_id;
    setRejectTarget(null);
    setBusyId(taskId);
    setActionError(null);
    try {
      await rejectTask(taskId, reason ?? "Declined by owner");
      refreshTasks();
      if (selectedTask?.task_id === taskId) {
        const updated = await getTask(taskId);
        await openTask(updated);
      }
    } catch (err: any) {
      setActionError(`Failed to reject task: ${err.message}`);
    } finally {
      setBusyId(null);
    }
  };

  const handleCancelConfirm = async () => {
    if (!cancelTarget) return;
    if (busyId !== null) return;
    const taskId = cancelTarget.task_id;
    setCancelTarget(null);
    setBusyId(taskId);
    setActionError(null);
    try {
      await cancelTask(taskId);
      refreshTasks();
      if (selectedTask?.task_id === taskId) {
        const updated = await getTask(taskId);
        await openTask(updated);
      }
    } catch (err: any) {
      setActionError(`Failed to cancel task: ${err.message}`);
    } finally {
      setBusyId(null);
    }
  };

  const handleNegotiateSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (negotiating) return;
    if (!selectedTask) return;
    let termsObj = {};
    try {
      termsObj = JSON.parse(counterTerms);
    } catch {
      setNegotiateError("Counter terms must be valid JSON.");
      return;
    }

    try {
      setNegotiating(true);
      setNegotiateError(null);
      await negotiateTask(selectedTask.task_id, {
        proposal_payload: termsObj,
        purpose: "negotiate-terms",
      });
      setNegotiateModalOpen(false);
      refreshTasks();
      const updated = await getTask(selectedTask.task_id);
      await openTask(updated);
    } catch (err: any) {
      setNegotiateError(`Failed to submit counter-offer: ${err.message}`);
    } finally {
      setNegotiating(false);
    }
  };

  const filters = [
    { id: "all", label: "All" },
    // `pending_approval` is the status that means "waiting for you". The old
    // filter offered "Negotiating", which is not a status the backend has, so
    // that tab was permanently empty.
    { id: "pending_approval", label: "Waiting on you" },
    { id: "accepted", label: "In progress" },
    { id: "waiting_remote", label: "Waiting on peer" },
    { id: "completed", label: "Completed" },
    { id: "failed", label: "Failed" },
  ];

  return (
    <PageShell 
      title="Tasks" 
      subtitle="Asynchronous task delegation, multi-round negotiations, and consent approvals"
      action={
        <Button
          size="sm"
          leftIcon={<Plus className="w-3.5 h-3.5" />}
          onClick={() => {
            setCreateError(null);
            setCreateModalOpen(true);
          }}
        >
          Delegate Task
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
          onClick={refreshTasks}
          disabled={loading}
        >
          Refresh
        </Button>
      </div>

      {loadError && <ErrorState message={loadError} onRetry={refreshTasks} />}
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
          <p className="text-sm text-neutral-400">Loading tasks...</p>
        </div>
      ) : loadError && tasks.length === 0 ? null : tasks.length === 0 ? (
        <div className="py-16 text-center">
          <p className="text-sm text-neutral-400">No tasks found in this view.</p>
          <p className="text-xs text-neutral-500 mt-1">
            Tasks represent delegated actions with remote agents.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-neutral-800">
          {tasks.map((task) => (
            <div key={task.task_id} className="py-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
              <div>
                <div className="flex items-center gap-2">
                  <StatusBadge status={task.status} />
                  <span className="text-sm font-medium text-neutral-100">
                    {task.task_type || "A2A task"}
                  </span>
                  {task.negotiation_round > 1 && (
                    <Badge variant="info">Round {task.negotiation_round}</Badge>
                  )}
                </div>
                <div className="text-xs text-neutral-400 mt-1.5 font-mono flex items-center gap-3">
                  <span>ID: {truncateId(task.task_id, 8)}</span>
                  <span className="font-sans">Peer: {resolveName(task.recipient_agent_id)}</span>
                  <span>{formatDate(task.created_at)}</span>
                </div>
              </div>

              <div className="flex items-center gap-2 shrink-0">
                {task.status === "pending_approval" && (
                  <>
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() => handleApprove(task.task_id)}
                      isLoading={busyId === task.task_id}
                      disabled={busyId !== null && busyId !== task.task_id}
                    >
                      Approve
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setRejectTarget(task)}
                      disabled={busyId !== null}
                    >
                      Reject
                    </Button>
                  </>
                )}
                {task.status === "accepted" && task.negotiation_round > 0 && (
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => {
                      setSelectedTask(task);
                      setTimeline([]);
                      setNegotiateModalOpen(true);
                    }}
                  >
                    Counter
                  </Button>
                )}
                {["pending", "pending_approval", "accepted", "waiting_remote"].includes(
                  task.status
                ) && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setCancelTarget(task)}
                    isLoading={busyId === task.task_id}
                    disabled={busyId !== null && busyId !== task.task_id}
                  >
                    Cancel
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => openTask(task)}
                >
                  Details
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Delegate Task Modal */}
      <Modal
        isOpen={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        title="Delegate New Task"
        subtitle="Initiate a secure task to a trusted peer"
      >
        <form onSubmit={handleCreateTask} className="space-y-4">
          {createError && (
            <p role="alert" className="text-xs text-red-400">
              {createError}
            </p>
          )}
          <div>
            <label className="text-xs text-neutral-400 font-medium block mb-1">
              Recipient Agent
            </label>
            {trustedAgents.length === 0 ? (
              <div className="p-3 bg-amber-950/20 text-xs text-amber-500">
                No trusted agents available.
              </div>
            ) : (
              <select
                value={recipientAgentId}
                onChange={(e) => setRecipientAgentId(e.target.value)}
                className="w-full bg-neutral-900 border border-neutral-800 rounded-md px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500 font-mono"
              >
                <option value="">-- Select Agent --</option>
                {trustedAgents.map((agt) => (
                  <option key={agt.agent_id} value={agt.agent_id}>
                    {agt.display_name} ({truncateId(agt.agent_id, 8)})
                  </option>
                ))}
              </select>
            )}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-neutral-400 font-medium block mb-1">
                Capability
              </label>
              {capsLoading ? (
                <p className="text-xs text-neutral-500 py-2" aria-busy="true">
                  Loading capabilities…
                </p>
              ) : capsError ? (
                <div className="flex items-center gap-2">
                  <p role="alert" className="text-xs text-red-400 flex-1">
                    Couldn&apos;t load capabilities: {capsError}
                  </p>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={reloadCaps}
                  >
                    Retry
                  </Button>
                </div>
              ) : capabilities.length === 0 ? (
                <p className="text-xs text-neutral-500 py-2">
                  No capabilities registered on this agent.
                </p>
              ) : (
                <>
                  <select
                    value={capabilityId}
                    onChange={(e) => pickCapability(e.target.value)}
                    className="w-full bg-neutral-900 border border-neutral-800 rounded-md px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500 font-mono"
                  >
                    <option value="">-- Select capability --</option>
                    {capabilities.map((cap) => (
                      <option key={cap.id} value={cap.id} title={cap.description}>
                        {cap.id}
                      </option>
                    ))}
                  </select>
                  {capabilityId !== "" && (
                    <p className="mt-1 text-[11px] text-neutral-500">
                      {taskType ? (
                        <>
                          Sends as task type{" "}
                          <span className="font-mono text-neutral-400">{taskType}</span>
                          {capabilities.find((c) => c.id === capabilityId)?.description
                            ? ` — ${capabilities.find((c) => c.id === capabilityId)?.description}`
                            : ""}
                        </>
                      ) : (
                        <span className="text-amber-500">
                          This capability has no task handler yet, so it cannot
                          be sent as a task.
                        </span>
                      )}
                    </p>
                  )}
                </>
              )}
            </div>
            <div>
              <label className="text-xs text-neutral-400 font-medium block mb-1">
                Purpose
              </label>
              <input
                type="text"
                value={purpose}
                onChange={(e) => setPurpose(e.target.value)}
                placeholder="e.g. Sync availability"
                maxLength={64}
                className="w-full bg-neutral-900 border border-neutral-800 rounded-md px-3 py-2 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>

          <div>
            <label className="text-xs text-neutral-400 font-medium block mb-1">
              Payload (JSON)
            </label>
            <textarea
              value={payloadText}
              onChange={(e) => setPayloadText(e.target.value)}
              rows={4}
              className="w-full bg-neutral-900 border border-neutral-800 rounded-md p-3 text-sm text-neutral-100 font-mono focus:outline-none focus:border-blue-500"
            />
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
              disabled={creating || !recipientAgentId || !taskType}
              isLoading={creating}
            >
              Dispatch Task
            </Button>
          </div>
        </form>
      </Modal>

      {/* Task Details Modal */}
      {selectedTask && (
        <Modal
          isOpen={!!selectedTask}
          onClose={() => setSelectedTask(null)}
          title="Task Details"
          subtitle={`Task ID: ${selectedTask.task_id}`}
          maxWidth="2xl"
        >
          <div className="space-y-4">
            <div className="flex items-center justify-between pb-3 border-b border-neutral-800">
              <div className="flex items-center gap-2">
                <StatusBadge status={selectedTask.status} />
                <Badge variant="outline">
                  Round {selectedTask.negotiation_round}
                </Badge>
              </div>
              <span className="text-xs text-neutral-400 font-mono">
                {formatDate(selectedTask.created_at)}
              </span>
            </div>

            <div className="grid grid-cols-2 gap-3 text-xs">
              <div>
                <label className="text-neutral-400 font-medium block mb-1">
                  Peer
                </label>
                <span className="font-mono text-neutral-300 break-all">
                  {resolveName(selectedTask.recipient_agent_id)}
                </span>
              </div>
              <div>
                <label className="text-neutral-400 font-medium block mb-1">
                  Type / purpose
                </label>
                <span className="font-mono text-neutral-300">
                  {selectedTask.task_type || "-"} · {selectedTask.purpose || "-"}
                </span>
              </div>
            </div>

            {selectedTask.failure_reason && (
              <div className="p-3 bg-red-950/30 border border-red-900/40 text-xs text-red-300">
                <span className="font-semibold">Failed:</span>{" "}
                {selectedTask.failure_reason}
              </div>
            )}

            {/* The negotiation timeline is the signed message audit for this
                task. It is empty when nothing has been exchanged yet, which is
                worth saying explicitly - an empty panel looks like a bug. */}
            <div>
              <label className="text-xs text-neutral-400 font-medium block mb-2">
                Signed messages for this task ({timeline.length})
              </label>
              {timeline.length === 0 ? (
                <p className="text-xs text-neutral-500">
                  No messages recorded for this task yet.
                </p>
              ) : (
                <div className="space-y-2 max-h-48 overflow-y-auto">
                  {timeline.map((message) => (
                    <div
                      key={message.id}
                      className="p-3 bg-neutral-900 border border-neutral-800 text-xs"
                    >
                      <div className="flex items-center justify-between text-neutral-400 font-mono mb-1">
                        <span>{message.message_type}</span>
                        <span>{formatDate(message.created_at)}</span>
                      </div>
                      <div className="text-[11px] text-neutral-500 font-mono break-all">
                        from {truncateId(message.sender_agent_id, 12)} ·{" "}
                        {message.status}
                        {message.policy_decision
                          ? ` · policy ${message.policy_decision}`
                          : ""}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div>
              <label className="text-xs text-neutral-400 font-medium block mb-1">
                Request payload
              </label>
              <pre className="p-3 bg-neutral-900 border border-neutral-800 text-xs text-neutral-300 font-mono overflow-x-auto">
                {JSON.stringify(selectedTask.request_payload ?? {}, null, 2)}
              </pre>
            </div>

            {selectedTask.response_payload != null && (
              <div>
                <label className="text-xs text-neutral-400 font-medium block mb-1">
                  Response payload
                </label>
                <pre className="p-3 bg-neutral-900 border border-neutral-800 text-xs text-neutral-300 font-mono overflow-x-auto">
                  {JSON.stringify(selectedTask.response_payload, null, 2)}
                </pre>
              </div>
            )}

            <div className="pt-3 border-t border-neutral-800 flex justify-end gap-2">
              {selectedTask.status === "accepted" && (
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => {
                    setNegotiateError(null);
                    setNegotiateModalOpen(true);
                  }}
                >
                  Counter-Offer
                </Button>
              )}
              {selectedTask.status === "pending_approval" && (
                <>
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => handleApprove(selectedTask.task_id)}
                    isLoading={busyId === selectedTask.task_id}
                    disabled={busyId !== null && busyId !== selectedTask.task_id}
                  >
                    Approve
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => setRejectTarget(selectedTask)}
                    disabled={busyId !== null}
                  >
                    Reject
                  </Button>
                </>
              )}
              <Button
                size="sm"
                variant="outline"
                onClick={() => setSelectedTask(null)}
              >
                Close
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* Counter-Offer Modal */}
      <Modal
        isOpen={negotiateModalOpen}
        onClose={() => setNegotiateModalOpen(false)}
        title="Submit Counter-Offer"
        subtitle="Propose adjusted parameters to peer agent"
      >
        <form onSubmit={handleNegotiateSubmit} className="space-y-4">
          {negotiateError && (
            <p role="alert" className="text-xs text-red-400">
              {negotiateError}
            </p>
          )}
          <div>
            <label className="text-xs text-neutral-400 font-medium block mb-1">
              Counter Terms (JSON)
            </label>
            <textarea
              value={counterTerms}
              onChange={(e) => setCounterTerms(e.target.value)}
              rows={4}
              className="w-full bg-neutral-900 border border-neutral-800 rounded-md p-3 text-sm text-neutral-100 font-mono focus:outline-none focus:border-blue-500"
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setNegotiateModalOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={negotiating}
              isLoading={negotiating}
            >
              Send
            </Button>
          </div>
        </form>
      </Modal>

      {/* Reject reason and cancel confirmation via modal, no native dialogs. */}
      <ConfirmModal
        open={rejectTarget !== null}
        title="Reject task"
        body={`Tell ${rejectTarget ? resolveName(rejectTarget.recipient_agent_id) : "the peer"} why this task is declined.`}
        confirmLabel="Reject task"
        danger
        requireReason
        onConfirm={(reason) => void handleRejectConfirm(reason)}
        onCancel={() => setRejectTarget(null)}
      />
      <ConfirmModal
        open={cancelTarget !== null}
        title="Cancel task"
        body="The task stops here. The peer is notified of the cancellation."
        confirmLabel="Cancel task"
        danger
        onConfirm={() => void handleCancelConfirm()}
        onCancel={() => setCancelTarget(null)}
      />
    </PageShell>
  );
}
